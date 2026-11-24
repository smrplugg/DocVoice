"""Самопроверка: всё ли работает на этом компьютере. Запускается из меню «Помощь»
или командой DocVoice.exe --selftest; результат пишется в журнал и logs/selftest.txt."""
from __future__ import annotations

import datetime
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import __version__
from .log import log, logs_dir, ram_gb
from .paths import data_dir


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    optional: bool = False     # не мешает основной работе (например, не скачан набор голосов)
    hint: str = ""             # что сделать человеку


def _run(name, fn, optional=False) -> Check:
    t = time.time()
    try:
        res = fn()
        ok, detail, hint = res if isinstance(res, tuple) and len(res) == 3 else (True, res or "", "")
        c = Check(name, ok, detail, optional, hint)
    except Exception as e:
        log.exception(f"Самопроверка «{name}»")
        c = Check(name, False, f"{type(e).__name__}: {e}", optional)
    c.detail = (c.detail + f" ({time.time() - t:.1f} с)").strip()
    return c


def run_checks(progress=None, stop_flag=None, settings=None) -> list[Check]:
    from .engines import ENGINES, create_engine
    from .settings import DEFAULTS, Settings

    s = settings.data if settings is not None else Settings().data
    tmp = data_dir() / "проверка-ёжик"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    results: list[Check] = []

    def step(name, fn, optional=False):
        if stop_flag and stop_flag():
            return
        if progress:
            progress(name)
        c = _run(name, fn, optional)
        results.append(c)
        log.info(f"Самопроверка: {'OK ' if c.ok else 'НЕТ'} {name}: {c.detail}")

    def writable():
        p = tmp / "запись.txt"
        p.write_text("ok", encoding="utf-8")
        p.unlink()
        return f"{'русские буквы в пути есть' if not str(data_dir()).isascii() else 'путь латиницей'}"

    def disk():
        free = shutil.disk_usage(data_dir()).free / 2 ** 30
        if free < 1:
            return False, f"свободно {free:.1f} ГБ", "Освободите место на диске: для записи книги нужно 1–2 ГБ."
        return f"свободно {free:.1f} ГБ"

    def memory():
        r = ram_gb()
        if not r:
            return "неизвестно"
        if r[0] < 3.5:
            return False, f"{r[0]:.1f} ГБ", ("Памяти мало: лучше пользоваться голосами Piper "
                                            "и не включать умные ударения.")
        return f"{r[0]:.1f} ГБ, свободно {r[1]:.1f} ГБ"

    def sound():
        import sounddevice as sd
        dev = sd.query_devices(kind="output")
        return f"«{dev['name']}»"

    def mp3():
        from .audio import _AudioOut
        p = tmp / "Проверка — Ёжик.mp3"
        sr = 22050
        f = _AudioOut(p, samplerate=sr, channels=1, format="MP3", subtype="MPEG_LAYER_III")
        t = np.arange(sr) / sr
        f.write((0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
        f.close()
        import soundfile as sf
        with open(p, "rb") as fh:
            dur = sf.info(fh).duration
        if dur < 0.5:
            return False, f"файл пустой ({dur:.1f} с)", ""
        return f"{p.stat().st_size // 1024} КБ"

    def pdf():
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        p = tmp / "Проверка книги «Ёжик».pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Proverka chteniya PDF", fontsize=14)
        doc.save(str(p))
        doc.close()
        from .extract import load_document
        d = load_document(str(p))
        if not d.lines:
            return False, "текст не найден", ""
        return f"строк: {len(d.lines)}"

    def docx():
        import docx as _docx
        p = tmp / "Проверка Word.docx"
        d = _docx.Document()
        d.add_heading("Глава первая", 1)
        d.add_paragraph("Проверка чтения документа Word.")
        d.save(str(p))
        from .extract import load_document
        doc = load_document(str(p))
        if len(doc.lines) < 2:
            return False, f"прочитано строк: {len(doc.lines)} из 2", ""
        return f"строк: {len(doc.lines)}"

    def engine_check(eid):
        cls = ENGINES[eid]

        def fn():
            ok, msg = cls.available()
            if not ok:
                return False, msg, ("Этот набор голосов не установлен — можно пользоваться другими."
                                    if "не установлен" in msg.lower() else msg)
            voices = cls.list_voices()
            if not voices:
                return False, "голоса не скачаны", "Нажмите «Голоса» и скачайте голос."
            want = s.get("voices", {}).get(eid)
            vi = next((v for v in voices if v.id == want), voices[0])
            vid, vname = vi.id, vi.name
            e = create_engine(eid)
            params = dict(DEFAULTS["params"])
            params.update({k: v for k, v in s.get("params", {}).items() if k.startswith("silero_")})
            e.load(vid, params)
            try:
                a = e.synth("Проверка голоса.")
            finally:
                e.close()
            sec = a.size / float(e.sample_rate or 1)
            if sec < 0.3:
                return False, f"голос «{vname}» вернул пустой звук", ""
            return f"голос «{vname}», {sec:.1f} с звука"
        return fn

    def smart_accent():
        from . import accent
        if not accent.installed():
            return False, "не скачаны", "При желании: «Голоса» → «Умные ударения» → «Скачать»."
        m = accent.get()
        if m is None:
            return False, accent.last_error() or "не загрузились", "Удалите и скачайте умные ударения заново."
        out = accent.accent_text("Он открыл замок ключом.")
        if "+" not in out:
            return False, f"ударения не поставлены: {out}", ""
        return out

    step("Папка данных доступна для записи", writable)
    step("Место на диске", disk)
    step("Оперативная память", memory, optional=True)
    step("Звук: колонки или наушники", sound)
    step("Запись MP3 с русским именем файла", mp3)
    step("Чтение PDF с русским именем файла", pdf)
    step("Чтение документа Word", docx)
    for eid, cls in ENGINES.items():
        step(f"Голоса: {cls.title}", engine_check(eid), optional=True)
    step("Умные ударения", smart_accent, optional=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return results


def format_results(results: list[Check]) -> str:
    lines = []
    for c in results:
        mark = "✔" if c.ok else ("•" if c.optional else "✘")
        lines.append(f"{mark} {c.name}: {c.detail}")
        if not c.ok and c.hint:
            lines.append(f"    → {c.hint}")
    return "\n".join(lines)


def write_report(results: list[Check]) -> Path:
    p = logs_dir() / "selftest.txt"
    p.write_text(f"Самопроверка DocVoice {__version__}, {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
                 + format_results(results) + "\n", encoding="utf-8")
    return p
