"""Воспроизведение, экспорт в MP3/OGG/WAV/FLAC и загрузка моделей."""
from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path

import numpy as np

from .engines import create_engine, render_paragraph
from .log import log

EXPORT_FORMATS = {
    "mp3": ("MP3", "MPEG_LAYER_III", "MP3 — подходит везде"),
    "ogg": ("OGG", "VORBIS", "OGG — меньше размер"),
    "flac": ("FLAC", "PCM_16", "FLAC — без потерь, большой файл"),
    "wav": ("WAV", "PCM_16", "WAV — без сжатия, очень большой файл"),
}
WRITE_CHUNK = 4096
# качество -> compression_level libsndfile (0 — лучшее качество)
QUALITY_LEVELS = {
    "Максимальное": 0.0,
    "Высокое": 0.25,
    "Среднее": 0.5,
    "Компактное": 0.75,
}


def _resample(a: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to or a.size == 0:
        return a
    n = int(round(a.size * sr_to / sr_from))
    x_old = np.linspace(0, 1, a.size, endpoint=False)
    x_new = np.linspace(0, 1, n, endpoint=False)
    return np.interp(x_new, x_old, a).astype(np.float32)


# ==========================================================================
# Плеер
# ==========================================================================
class Player:
    """Синтез в фоновом потоке + вывод через sounddevice. GUI опрашивает состояние."""

    PREFETCH = 3

    def __init__(self):
        self.state = "stopped"        # stopped | loading | playing | paused
        self.current = -1             # индекс абзаца, который сейчас звучит
        self.error: str | None = None
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue(maxsize=self.PREFETCH)
        self._thread: threading.Thread | None = None
        self._stream = None
        self._buf = np.zeros(0, np.float32)
        self._buf_idx = -1
        self._synth_done = False
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self.device = None

    # ---- управление
    def play(self, paragraphs: list[str], start: int, engine_id: str, voice_id: str,
             params: dict, text_opts: dict, device=None):
        self.stop()
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._q = queue.Queue(maxsize=self.PREFETCH)
        self._buf = np.zeros(0, np.float32)
        self._buf_idx = -1
        self._synth_done = False
        self.error = None
        self.current = start
        self.state = "loading"
        self.device = device
        log.info(f"Чтение: {engine_id} / {voice_id}, скорость {params.get('speed')}, "
                 f"с абзаца {start + 1} из {len(paragraphs)}")
        self._thread = threading.Thread(
            target=self._synth_loop,
            args=(paragraphs, start, engine_id, voice_id, params, text_opts),
            daemon=True)
        self._thread.start()

    def pause(self):
        if self.state == "playing":
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "playing"

    def stop(self):
        self._stop.set()
        st = self._stream
        self._stream = None
        if st is not None:
            try:
                st.abort()
                st.close()
            except Exception:
                pass
        # разблокировать поток синтеза, если он ждёт место в очереди
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self.state = "stopped"

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    # ---- поток синтеза
    def _synth_loop(self, paragraphs, start, engine_id, voice_id, params, text_opts):
        engine = None
        try:
            engine = create_engine(engine_id)
            engine.load(voice_id, params)
            stop_flag = self._stop.is_set
            for i in range(start, len(paragraphs)):
                if self._stop.is_set():
                    return
                audio = render_paragraph(engine, paragraphs[i], text_opts, params, stop_flag)
                if self._stop.is_set():
                    return
                if self._stream is None:
                    self.sr = self._open_stream(engine.sample_rate)
                if engine.sample_rate != self.sr:
                    audio = _resample(audio, engine.sample_rate, self.sr)
                while not self._stop.is_set():
                    try:
                        self._q.put((i, audio), timeout=0.2)
                        break
                    except queue.Full:
                        continue
            self._synth_done = True
            if self._stream is None and not self._stop.is_set():
                # нечего играть
                self.state = "stopped"
                self._finished.set()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("Ошибка при чтении вслух")
            self._synth_done = True
            self.stop()
            self._finished.set()
        finally:
            if engine is not None:
                try:
                    engine.close()
                except Exception:
                    pass

    def _open_stream(self, sr: int) -> int:
        """Открывает вывод; если устройство не принимает частоту голоса или
        сохранённое устройство пропало — пробует 48/44.1 кГц и устройство по умолчанию.
        Возвращает фактическую частоту."""
        import sounddevice as sd
        tries = []
        for dev in ([self.device, None] if self.device is not None else [None]):
            for rate in (sr, 48000, 44100):
                if (dev, rate) not in tries:
                    tries.append((dev, rate))
        last = None
        for dev, rate in tries:
            try:
                stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                         callback=self._callback, finished_callback=self._on_finished,
                                         device=dev, blocksize=2048, latency="high")
                stream.start()
            except Exception as e:
                last = e
                continue
            self._stream = stream
            self.state = "playing" if self.state == "loading" else self.state
            return rate
        log.error(f"Звук: не открылось ни одно устройство вывода (последняя ошибка: {last})")
        raise RuntimeError("Не получилось включить звук: колонки или наушники не найдены, выключены "
                           f"или заняты другой программой. Проверьте их и попробуйте ещё раз. ({last})")

    def _callback(self, outdata, frames, time_info, status):
        import sounddevice as sd
        out = outdata[:, 0]
        if self.state == "paused" or self._stop.is_set():
            out[:] = 0
            return
        pos = 0
        while pos < frames:
            if self._buf.size == 0:
                try:
                    idx, audio = self._q.get_nowait()
                    self._buf = audio
                    self._buf_idx = idx
                    self.current = idx
                    continue
                except queue.Empty:
                    if self._synth_done:
                        out[pos:] = 0
                        raise sd.CallbackStop()
                    out[pos:] = 0  # ждём синтез
                    return
            n = min(frames - pos, self._buf.size)
            out[pos:pos + n] = self._buf[:n]
            self._buf = self._buf[n:]
            pos += n

    def _on_finished(self):
        if not self._stop.is_set():
            self.state = "stopped"
            self._finished.set()


# ==========================================================================
# Экспорт
# ==========================================================================
SPLIT_MODES = {
    "none": "Не разбивать — один файл",
    "chapters": "По главам — файл на каждую главу",
    "minutes": "На части одинаковой длины",
    "chapters_minutes": "По главам, длинные главы — ещё на части",
}

_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*‪-‮⁦-⁩]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


def safe_filename(name: str, max_len: int = 100) -> str:
    """Имя файла для Windows с сохранением кириллицы и любых букв Юникода:
    убираем только запрещённые символы, точки/пробелы в конце и
    зарезервированные имена (CON, NUL, …)."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace(":", " -").replace('"', "'").replace("/", "-").replace("\\", "-")
    name = _BAD_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0] if " " in name[:max_len] else name[:max_len]
    name = name.rstrip(" .")
    if _RESERVED.match(name):
        name = "_" + name
    return name or "audio"


def part_path(base: Path, label: str, title: str = "") -> Path:
    """«Книга 03 - Глава третья.mp3»; длина пути не выходит за MAX_PATH Windows."""
    room = max(20, 240 - len(str(base.parent)) - len(base.stem) - len(label) - len(base.suffix) - 5)
    name = f"{base.stem} {label}"
    if title:
        name += " - " + safe_filename(title, min(80, room))
    return base.with_name(safe_filename(name, 200) + base.suffix)


class EtaEstimator:
    """Оставшееся время синтеза без скачков.

    Скорость (символов в секунду) — смесь трёх оценок: прошлые запуски этим
    голосом (prior, как будто это первые 30 с наблюдения), средняя за всё время
    и скорость за последние 2 минуты (если компьютер занялся другим).
    Показанное время плавно идёт вниз и подтягивается к расчётному, а не прыгает."""

    PRIOR_SEC = 30.0
    WINDOW_SEC = 120.0

    def __init__(self, total: int, prior_rate: float | None = None):
        self.total = max(1, total)
        self.prior = prior_rate if prior_rate and prior_rate > 0 else None
        self.t0 = time.time()
        self.hist: list[tuple[float, int]] = [(self.t0, 0)]
        self.shown: float | None = None
        self.shown_t = self.t0

    def rate(self, done: int, now: float) -> float | None:
        el = now - self.t0
        if self.prior is None and (el < 5 or done <= 0):
            return None
        r = (done + (self.prior or 0) * self.PRIOR_SEC) / (el + (self.PRIOR_SEC if self.prior else 0))
        while len(self.hist) > 2 and now - self.hist[1][0] > self.WINDOW_SEC:
            self.hist.pop(0)
        t_old, d_old = self.hist[0]
        if now - t_old >= 20 and done > d_old and el > self.WINDOW_SEC:
            r = 0.5 * r + 0.5 * (done - d_old) / (now - t_old)
        return r if r > 0 else None

    def update(self, done: int, now: float | None = None) -> float | None:
        now = now or time.time()
        self.hist.append((now, done))
        r = self.rate(done, now)
        if r is None:
            return None
        raw = (self.total - done) / r
        if self.shown is None:
            self.shown = raw
        else:
            ticked = max(0.0, self.shown - (now - self.shown_t))
            # небольшие расхождения не показываем, большие — догоняем на четверть
            self.shown = ticked if abs(raw - ticked) <= 0.1 * max(ticked, 30) else ticked + 0.25 * (raw - ticked)
        self.shown_t = now
        return self.shown


class _AudioOut:
    """Аудиофайл на запись. Файл открывает Python, а libsndfile получает уже
    открытый поток: иначе часть сборок libsndfile/soundfile получает путь в
    UTF-8, а Windows читает его как cp1251 — «Глава» превращается в «Р“Р»Р°РІР°»."""

    def __init__(self, path, **kw):
        import soundfile as sf
        self.fh = open(path, "w+b")
        try:
            self.sf = sf.SoundFile(self.fh, "w", **kw)
        except Exception:
            self.fh.close()
            raise

    def write(self, data):
        self.sf.write(data)

    def close(self):
        try:
            self.sf.close()
        finally:
            self.fh.close()


def export_audio(paragraphs: list[str], out_path: str, fmt: str, quality: float,
                 engine_id: str, voice_id: str, params: dict, text_opts: dict,
                 split_minutes: float = 0, title: str = "", artist: str = "",
                 progress=None, stop_flag=None, split_mode: str | None = None,
                 prior_rate: float | None = None,
                 chapters: list[tuple[int, str]] | None = None) -> list[str]:
    """Синтезирует абзацы и пишет в файл(ы). Возвращает список созданных файлов.

    split_mode: none | minutes | chapters | chapters_minutes (см. SPLIT_MODES);
    chapters — [(индекс первого абзаца, название)] для деления по главам."""
    import soundfile as sf
    fmt = fmt.lower()
    sf_format, subtype, _ = EXPORT_FORMATS[fmt]
    if sf_format not in sf.available_formats():
        raise RuntimeError(f"Ваша версия libsndfile не умеет писать {sf_format}. "
                           f"Обновите пакет soundfile (pip install -U soundfile).")
    if split_mode is None:
        split_mode = "minutes" if split_minutes else "none"
    by_chapters = split_mode in ("chapters", "chapters_minutes") and bool(chapters)
    if split_mode == "chapters_minutes" and not chapters:
        split_mode = "minutes"      # глав не нашлось — делим хотя бы по времени
    by_minutes = split_mode in ("minutes", "chapters_minutes") and split_minutes and split_minutes > 0
    ch_at = {s: t for s, t in (chapters or [])} if by_chapters else {}
    if by_chapters and 0 not in ch_at:
        ch_at[0] = title or "Вступление"
    n_ch = len(ch_at)
    ch_width = max(2, len(str(n_ch)))

    log.info(f"Запись: {engine_id} / {voice_id}, {fmt}, абзацев {len(paragraphs)}, "
             f"деление: {split_mode}, глав {len(chapters or [])}")
    t_start = time.time()
    engine = create_engine(engine_id)
    engine.load(voice_id, params)
    files: list[str] = []
    f = None
    sr = None
    frames_in_part = 0
    split_frames = None
    base = Path(out_path)
    base = base.with_name(safe_filename(base.stem, 150) + base.suffix)
    part = 0             # сквозной номер файла
    ch_no = 0            # номер главы
    ch_title = ""
    ch_files: list[str] = []   # файлы текущей главы
    ch_part = 0
    total_chars = sum(len(p) for p in paragraphs) or 1
    done_chars = 0
    eta_est = EtaEstimator(total_chars, prior_rate)

    def finish_chapter():
        """Закрывает файл главы; если глава уместилась в один файл —
        убирает из имени номер части."""
        nonlocal f
        if f is not None:
            f.close()
            f = None
        if by_chapters and by_minutes and len(ch_files) == 1:
            src = Path(ch_files[0])
            dst = part_path(base, f"{ch_no:0{ch_width}d}", ch_title)
            try:
                if not dst.exists():
                    os.replace(src, dst)
                    files[files.index(str(src))] = str(dst)
            except OSError:
                pass

    def open_part(new_chapter: bool = False):
        nonlocal f, part, frames_in_part, ch_no, ch_title, ch_part
        if f is not None:
            f.close()
            f = None
        part += 1
        if by_chapters:
            if new_chapter:
                ch_part = 0
                ch_files.clear()
            ch_part += 1
            label = f"{ch_no:0{ch_width}d}" + (f".{ch_part}" if by_minutes else "")
            path = part_path(base, label, ch_title)
        elif by_minutes:
            path = part_path(base, f"{part:03d}")
        else:
            path = base
        kw = {}
        if fmt in ("mp3", "ogg"):
            kw["compression_level"] = float(quality)
        if fmt == "mp3":
            kw["bitrate_mode"] = "CONSTANT"
        f = _AudioOut(path, samplerate=sr, channels=1, format=sf_format, subtype=subtype, **kw)
        # Название в теги не пишем: плееры и Telegram тогда показывают имя файла.
        # MP3: libsndfile пишет только ID3v1 (латиница, 30 байт) — кириллица там
        # превращается в «Ð»…, поэтому теги MP3 пишем сами после записи (write_id3).
        # WAV: в INFO-чанке кодировку никто не понимает одинаково — только латиница.
        if fmt in ("ogg", "flac"):       # Vorbis-комментарии — честный UTF-8
            tags = [("artist", artist), ("software", "DocVoice")]
            if by_chapters or by_minutes:
                tags += [("album", title or base.stem), ("tracknumber", str(part))]
        elif fmt == "wav":
            tags = [("software", "DocVoice")]
        else:
            tags = []
        for attr, val in tags:
            if val:
                try:
                    setattr(f.sf, attr, val)
                except Exception:
                    pass
        files.append(str(path))
        if by_chapters:
            ch_files.append(str(path))
        frames_in_part = 0

    try:
        for i, p in enumerate(paragraphs):
            if stop_flag and stop_flag():
                break
            audio = render_paragraph(engine, p, text_opts, params, stop_flag)
            if sr is None:
                sr = engine.sample_rate
                split_frames = int(split_minutes * 60 * sr) if by_minutes else None
            elif engine.sample_rate != sr:
                audio = _resample(audio, engine.sample_rate, sr)
            if i in ch_at:
                finish_chapter()
                ch_no += 1
                ch_title = ch_at[i]
                open_part(True)
            elif f is None:
                open_part()
            elif split_frames and frames_in_part >= split_frames:
                open_part()
            if audio.size:
                # маленькими порциями: libsndfile (Vorbis) кладёт буфер на стек,
                # крупная запись роняет процесс (особенно на Windows со стеком 1 МБ)
                for k in range(0, audio.size, WRITE_CHUNK):
                    f.write(audio[k:k + WRITE_CHUNK])
                frames_in_part += audio.size
            done_chars += len(p)
            if progress:
                progress(i + 1, len(paragraphs), eta_est.update(done_chars))
    finally:
        if f is not None:
            f.close()
            f = None
        engine.close()
    if by_chapters and not (stop_flag and stop_flag()):
        finish_chapter()
    if stop_flag and stop_flag():
        # отменено — удаляем недописанные файлы
        for fp in files:
            try:
                os.unlink(fp)
            except OSError:
                pass
        log.info("Запись отменена")
        return []
    log.info(f"Запись готова: файлов {len(files)}, за {time.time() - t_start:.0f} с")
    if fmt == "mp3":
        album = (title or base.stem) if len(files) > 1 else ""
        for k, fp in enumerate(files):
            try:
                write_id3(fp, artist=artist, album=album,
                          track=f"{k + 1}/{len(files)}" if len(files) > 1 else "")
            except OSError:
                pass
    return files


def _id3_text_frame(fid: str, text: str) -> bytes:
    data = b"\x01" + text.encode("utf-16") + b"\x00\x00"     # 1 = UTF-16 с BOM
    return fid.encode("ascii") + len(data).to_bytes(4, "big") + b"\x00\x00" + data


def write_id3(path: str, artist: str = "", album: str = "", track: str = "") -> None:
    """Дописывает в начало MP3 тег ID3v2.3 в UTF-16 — так кириллицу правильно
    показывают Windows, телефоны и Telegram. Пустые поля не пишутся; если писать
    нечего — файл не трогаем."""
    frames = b"".join(_id3_text_frame(fid, val) for fid, val in
                      (("TPE1", artist), ("TALB", album), ("TRCK", track)) if val)
    if not frames:
        return
    n = len(frames)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])   # synchsafe
    tmp = path + ".tag"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(b"ID3\x03\x00\x00" + size + frames)
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.replace(tmp, path)


# ==========================================================================
# Загрузка файлов
# ==========================================================================
def download(url: str, dest: Path, progress=None, stop_flag=None, timeout: int = 30):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "DocVoice/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if stop_flag and stop_flag():
                break
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    if stop_flag and stop_flag():
        tmp.unlink(missing_ok=True)
        raise InterruptedError("Отменено")
    shutil.move(str(tmp), str(dest))
    return dest
