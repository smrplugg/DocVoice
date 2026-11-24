"""Офлайн-движки синтеза речи: Piper, Silero, голоса Windows (SAPI/OneCore)."""
from __future__ import annotations

import json
import math
import os
import re
import sys
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from .log import log
from .paths import data_dir, piper_dir, silero_dir
from . import textproc


@dataclass
class VoiceInfo:
    id: str
    name: str
    lang: str = ""
    extra: dict = field(default_factory=dict)


class EngineError(Exception):
    pass


VCREDIST_HINT = ("Обычно не хватает системной библиотеки Windows — установите «Microsoft Visual C++ "
                 "Redistributable 2015-2022 (x64)» с сайта Microsoft и перезапустите программу.")


def _import_problem(what: str, e: Exception) -> str:
    msg = f"{what} не загрузился: {type(e).__name__}: {e}"
    if "DLL" in str(e) or isinstance(e, OSError):
        msg += ". " + VCREDIST_HINT
    return msg


# --------------------------------------------------------------------------
# espeak (произношение для Piper) и не латинские буквы в пути
# --------------------------------------------------------------------------
_espeak_dir: Path | None = None


def _short_path(p: Path) -> str | None:
    """Короткое имя пути Windows (C:\\Users\\8C5C~1\\…) — только латиница."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
        return buf.value if n else None
    except Exception:
        return None


def _ascii_base_dirs() -> list[Path]:
    out = [data_dir()]
    for env in ("ProgramData", "PUBLIC"):
        if os.environ.get(env):
            out.append(Path(os.environ[env]) / "DocVoice")
    out.append(Path(os.environ.get("SystemDrive", "C:") + "\\") / "DocVoice")
    return [p for p in out if str(p).isascii()]


def espeak_data_dir() -> Path:
    """Папка данных espeak для Piper. espeak открывает файлы обычным fopen и не
    понимает не латинские буквы в пути: у программы в «C:\\Users\\Иван\\Desktop»
    голоса Piper не работали бы (а espeak при ошибке закрывает всю программу).
    Тогда берём короткое имя пути Windows или один раз копируем данные (~19 МБ)
    в папку с латинским путём."""
    global _espeak_dir
    if _espeak_dir is not None:
        return _espeak_dir
    from piper.phonemize_espeak import ESPEAK_DATA_DIR
    src = Path(ESPEAK_DATA_DIR)
    result = src
    if not str(src).isascii():
        short = _short_path(src)
        if short and short.isascii() and Path(short, "phontab").is_file():
            result = Path(short)
            log.info("espeak: путь с не латинскими буквами, использую короткое имя пути")
        else:
            stamp = f"{src.stat().st_mtime:.0f}"
            for base in _ascii_base_dirs():
                dst = base / "espeak-ng-data"
                marker = dst / ".docvoice-copy"
                try:
                    if not marker.is_file() or marker.read_text() != stamp:
                        tmp = base / "espeak-ng-data.tmp"
                        shutil.rmtree(tmp, ignore_errors=True)
                        shutil.copytree(src, tmp)
                        (tmp / ".docvoice-copy").write_text(stamp)
                        shutil.rmtree(dst, ignore_errors=True)
                        os.replace(tmp, dst)
                    result = dst
                    log.info(f"espeak: путь с не латинскими буквами, данные скопированы в {dst}")
                    break
                except OSError as e:
                    log.warning(f"espeak: не удалось скопировать данные в {base}: {e}")
            else:
                log.error("espeak: путь к программе содержит не латинские буквы, а латинской папки "
                          "для данных не нашлось — голоса Piper могут не работать")
    _espeak_dir = result
    return result


class BaseEngine:
    id = "base"
    title = "base"
    needs_numbers = False      # движок не умеет читать цифры сам
    latin_ok = True            # умеет читать латиницу
    allowed_chars = None       # regex символов, которые надо выкинуть
    max_chars = 1000
    supports_pitch = False
    stress_style = "none"      # как отметить ударение: plus / acute / none (textproc.apply_stress_style)
    description = ""

    def __init__(self):
        self.sample_rate = 22050

    # ---- статические сведения
    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, ""

    @classmethod
    def list_voices(cls) -> list[VoiceInfo]:
        return []

    @classmethod
    def caps(cls) -> dict:
        return {"needs_numbers": cls.needs_numbers, "latin_ok": cls.latin_ok,
                "allowed_chars": cls.allowed_chars}

    # ---- работа
    def load(self, voice_id: str, params: dict):
        raise NotImplementedError

    def synth(self, text: str) -> np.ndarray:
        """Возвращает float32 моно [-1, 1] с частотой self.sample_rate."""
        raise NotImplementedError

    def close(self):
        pass


# ==========================================================================
# Piper
# ==========================================================================
_piper_cache: dict[str, object] = {}
_piper_lock = threading.Lock()


class PiperEngine(BaseEngine):
    id = "piper"
    title = "Piper — быстрые голоса"
    stress_style = "acute"     # espeak понимает знак ударения над гласной
    max_chars = 600
    description = ("Быстрые нейросетевые голоса, работают на любом процессоре. "
                   "Голоса скачиваются в «Управление голосами».")

    @classmethod
    def available(cls):
        try:
            import piper  # noqa
            return True, ""
        except ImportError as e:
            if getattr(sys, "frozen", False) or "DLL" in str(e):
                return False, _import_problem("Piper", e)
            return False, f"Не установлен пакет piper-tts ({e}). Выполните: pip install piper-tts"
        except Exception as e:
            return False, _import_problem("Piper", e)

    @classmethod
    def list_voices(cls) -> list[VoiceInfo]:
        res = []
        for onnx in sorted(piper_dir().glob("*.onnx")):
            cfg_path = onnx.with_suffix(".onnx.json")
            if not cfg_path.exists():
                continue
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
            lang = (cfg.get("language") or {}).get("code", "") or onnx.stem.split("-")[0]
            parts = onnx.stem.split("-")
            nice = parts[1] if len(parts) >= 3 else onnx.stem
            quality = parts[2] if len(parts) >= 3 else ""
            base_name = f"{nice} ({lang}, {quality})" if quality else f"{nice} ({lang})"
            spk = cfg.get("speaker_id_map") or {}
            if 1 < len(spk) <= 30:
                for sname, sid in sorted(spk.items(), key=lambda x: x[1]):
                    res.append(VoiceInfo(f"{onnx.name}#{sid}", f"{base_name} — {sname}", lang))
            else:
                res.append(VoiceInfo(onnx.name, base_name, lang))
        return res

    def load(self, voice_id: str, params: dict):
        from piper import PiperVoice, SynthesisConfig
        fname, _, spk = voice_id.partition("#")
        path = piper_dir() / fname
        if not path.exists():
            raise EngineError(f"Файл голоса не найден: {path}")
        key = str(path)
        with _piper_lock:
            voice = _piper_cache.get(key)
            if voice is None:
                t = time.time()
                voice = PiperVoice.load(str(path), espeak_data_dir=espeak_data_dir())
                _piper_cache[key] = voice
                log.info(f"Piper: голос {fname} загружен за {time.time() - t:.1f} с")
        self.voice = voice
        self.sample_rate = int(voice.config.sample_rate)
        speed = max(0.3, float(params.get("speed", 1.0)))
        base_ls = getattr(voice.config, "length_scale", 1.0) or 1.0
        kw = dict(length_scale=base_ls / speed)
        if params.get("piper_noise_scale") is not None:
            kw["noise_scale"] = float(params["piper_noise_scale"])
        if params.get("piper_noise_w") is not None:
            kw["noise_w_scale"] = float(params["piper_noise_w"])
        if spk:
            kw["speaker_id"] = int(spk)
        self.syn_config = SynthesisConfig(**kw)

    def synth(self, text: str) -> np.ndarray:
        parts = []
        with _piper_lock:
            for ch in self.voice.synthesize(text, self.syn_config):
                parts.append(ch.audio_float_array.astype(np.float32))
        if not parts:
            return np.zeros(0, np.float32)
        return np.concatenate(parts)


# ==========================================================================
# Silero
# ==========================================================================
SILERO_MODELS = {
    "v5_6_ru": ("https://models.silero.ai/models/tts/ru/v5_6_ru.pt",
                "Русский v5.6 — новейшая, авто-ударения, омографы, интонации вопросов"),
    "v5_ru": ("https://models.silero.ai/models/tts/ru/v5_ru.pt",
              "Русский v5 — авто-ударения и омографы"),
    "v4_ru": ("https://models.silero.ai/models/tts/ru/v4_ru.pt",
              "Русский v4 — предыдущее поколение, чуть быстрее"),
}
SILERO_SPEAKERS = ["xenia", "baya", "kseniya", "aidar", "eugene"]
SILERO_SPEAKER_NAMES = {"xenia": "Ксения (жен.)", "baya": "Байя (жен.)", "kseniya": "Ксения-2 (жен.)",
                        "aidar": "Айдар (муж.)", "eugene": "Евгений (муж.)"}

_silero_cache: dict[str, object] = {}
_silero_lock = threading.Lock()


class SileroEngine(BaseEngine):
    id = "silero"
    title = "Silero — самые живые голоса"
    stress_style = "plus"
    needs_numbers = True
    latin_ok = False
    allowed_chars = r"[^0-9a-zA-Zа-яёА-ЯЁ+\s.,!?:;\-—–\"'()]"
    max_chars = 800
    supports_pitch = True
    description = ("Очень естественный русский, сам ставит ударения. Нужен PyTorch "
                   "(≈200 МБ) и модель (≈100 МБ). Лицензия моделей — некоммерческая (CC BY-NC).")

    @classmethod
    def available(cls):
        try:
            import torch  # noqa
            return True, ""
        except ImportError as e:
            if getattr(sys, "frozen", False) or "DLL" in str(e):
                return False, _import_problem("PyTorch", e)
            return False, ("Не установлен PyTorch. Запустите install_silero.bat "
                           "(или pip install torch --index-url https://download.pytorch.org/whl/cpu)")
        except Exception as e:
            return False, _import_problem("PyTorch", e)

    @classmethod
    def list_voices(cls) -> list[VoiceInfo]:
        res = []
        for mid in SILERO_MODELS:
            if (silero_dir() / f"{mid}.pt").exists():
                spk = SILERO_SPEAKERS if mid != "v4_ru" else [s for s in SILERO_SPEAKERS if s != "eugene"]
                for s in spk:
                    res.append(VoiceInfo(f"{mid}|{s}", f"{SILERO_SPEAKER_NAMES.get(s, s)} — {mid}", "ru"))
        return res

    def load(self, voice_id: str, params: dict):
        import torch
        import torch.package  # явно — для собранного exe
        mid, _, speaker = voice_id.partition("|")
        path = silero_dir() / f"{mid}.pt"
        if not path.exists():
            raise EngineError(f"Модель Silero {mid} не скачана")
        with _silero_lock:
            model = _silero_cache.get(str(path))
            if model is None:
                threads = int(params.get("silero_threads", 0) or 0) or max(1, min(8, (os.cpu_count() or 4)))
                torch.set_num_threads(threads)
                # torch открывает путь через fopen и не понимает кириллицу в пути
                # к программе на Windows — отдаём ему уже открытый файл
                t = time.time()
                with open(path, "rb") as fh:
                    model = torch.package.PackageImporter(fh).load_pickle("tts_models", "model")
                log.info(f"Silero: модель {mid} загружена за {time.time() - t:.1f} с, потоков {threads}")
                model.to(torch.device("cpu"))
                _silero_cache[str(path)] = model
        self.model = model
        self.speaker = speaker or "xenia"
        self.sample_rate = int(params.get("silero_sample_rate", 48000))
        self.put_accent = bool(params.get("silero_accent", True))
        self.put_yo = bool(params.get("silero_yo", True))
        speed = float(params.get("speed", 1.0))
        pitch = float(params.get("pitch", 0.0))
        self.rate_word = None
        if abs(speed - 1.0) > 0.04:
            self.rate_word = ("x-slow" if speed < 0.7 else "slow" if speed < 0.92 else
                              "medium" if speed < 1.1 else "fast" if speed < 1.4 else "x-fast")
        self.pitch_word = None
        if abs(pitch) > 0.05:
            self.pitch_word = ("x-low" if pitch < -0.6 else "low" if pitch < 0 else
                               "high" if pitch < 0.6 else "x-high")

    def synth(self, text: str) -> np.ndarray:
        import torch
        text = text.strip()
        if not re.search(r"[а-яёА-ЯЁa-zA-Z]", text):
            return np.zeros(0, np.float32)
        kw = dict(speaker=self.speaker, sample_rate=self.sample_rate)
        if self.rate_word or self.pitch_word:
            attrs = ""
            if self.rate_word and self.rate_word != "medium":
                attrs += f' rate="{self.rate_word}"'
            if self.pitch_word:
                attrs += f' pitch="{self.pitch_word}"'
            kw["ssml_text"] = f"<speak><prosody{attrs}>{escape(text)}</prosody></speak>"
        else:
            kw["text"] = text
        with _silero_lock, torch.no_grad():
            try:
                audio = self.model.apply_tts(put_accent=self.put_accent, put_yo=self.put_yo, **kw)
            except TypeError:
                audio = self.model.apply_tts(**kw)
            except Exception as e:
                # SSML иногда падает на экзотике — пробуем обычным текстом
                if "ssml_text" in kw:
                    kw.pop("ssml_text")
                    kw["text"] = text
                    audio = self.model.apply_tts(**kw)
                else:
                    raise EngineError(f"Silero: {e}") from e
        return audio.detach().cpu().numpy().astype(np.float32)


# ==========================================================================
# Windows SAPI / OneCore
# ==========================================================================
ONECORE_KEY = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices"
SAPI_KEY = r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices"


def _sapi_tokens():
    import win32com.client
    tokens = []
    seen = set()
    for key in (SAPI_KEY, ONECORE_KEY):
        try:
            cat = win32com.client.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(key, False)
            col = cat.EnumerateTokens()
            for i in range(col.Count):
                t = col.Item(i)
                desc = t.GetDescription()
                if desc in seen:
                    continue
                seen.add(desc)
                tokens.append(t)
        except Exception:
            continue
    return tokens


class SapiEngine(BaseEngine):
    id = "sapi"
    title = "Голоса Windows"
    max_chars = 1500
    supports_pitch = True
    description = ("Голоса, установленные в Windows (Ирина, Павел и др.). Русские голоса "
                   "добавляются в Параметры → Время и язык → Речь.")

    @classmethod
    def available(cls):
        if sys.platform != "win32":
            return False, "Только для Windows"
        try:
            import win32com.client  # noqa
            return True, ""
        except Exception:
            return False, "Не установлен pywin32 (pip install pywin32)"

    @classmethod
    def list_voices(cls) -> list[VoiceInfo]:
        ok, _ = cls.available()
        if not ok:
            return []
        import pythoncom
        pythoncom.CoInitialize()
        res = []
        try:
            for t in _sapi_tokens():
                try:
                    lang = t.GetAttribute("Language")
                except Exception:
                    lang = ""
                lang = _lcid_to_lang(lang)
                res.append(VoiceInfo(t.Id, t.GetDescription(), lang))
        except Exception:
            pass
        return res

    def load(self, voice_id: str, params: dict):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self.sp = win32com.client.Dispatch("SAPI.SpVoice")
        for t in _sapi_tokens():
            if t.Id == voice_id:
                self.sp.Voice = t
                break
        speed = max(0.3, float(params.get("speed", 1.0)))
        # SAPI: rate -10..10, +10 ≈ в 3 раза быстрее
        self.sp.Rate = int(max(-10, min(10, round(10 * math.log(speed) / math.log(3)))))
        self.sp.Volume = 100
        self.pitch = int(max(-10, min(10, round(float(params.get("pitch", 0.0)) * 10))))
        self.sample_rate = 22050

    def synth(self, text: str) -> np.ndarray:
        import win32com.client
        import soundfile as sf
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            fs = win32com.client.Dispatch("SAPI.SpFileStream")
            try:
                fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
                fmt.Type = 22  # SAFT22kHz16BitMono
                fs.Format = fmt
            except Exception:
                try:
                    fs.Format.Type = 22
                except Exception:
                    pass  # оставим формат по умолчанию — частоту прочитаем из файла
            fs.Open(tmp, 3, False)  # SSFMCreateForWrite
            self.sp.AudioOutputStream = fs
            xml = f'<pitch absmiddle="{self.pitch}">{escape(text)}</pitch>' if self.pitch else escape(text)
            self.sp.Speak(xml, 8)  # SVSFIsXML
            fs.Close()
            self.sp.AudioOutputStream = None
            with open(tmp, "rb") as fh:   # TEMP может быть в профиле с кириллицей
                data, sr = sf.read(fh, dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            self.sample_rate = sr
            return data.astype(np.float32)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def close(self):
        self.sp = None


def _lcid_to_lang(s: str) -> str:
    try:
        lcid = int(s.split(";")[0], 16)
    except Exception:
        return s
    return {0x419: "ru", 0x409: "en-US", 0x809: "en-GB", 0x422: "uk", 0x407: "de",
            0x40c: "fr", 0x410: "it", 0x40a: "es", 0xc0a: "es", 0x411: "ja", 0x804: "zh"}.get(lcid, s)


ENGINES: dict[str, type[BaseEngine]] = {
    "piper": PiperEngine,
    "silero": SileroEngine,
    "sapi": SapiEngine,
}


def create_engine(engine_id: str) -> BaseEngine:
    cls = ENGINES.get(engine_id)
    if cls is None:
        raise EngineError(f"Неизвестный движок {engine_id}")
    ok, msg = cls.available()
    if not ok:
        raise EngineError(msg)
    return cls()


# ==========================================================================
# синтез абзаца с паузами и нормализацией
# ==========================================================================
def silence(ms: float, sr: int) -> np.ndarray:
    return np.zeros(max(0, int(sr * ms / 1000.0)), np.float32)


def trim_silence(a: np.ndarray, thr: float = 0.004, keep_ms: float = 30, sr: int = 22050) -> np.ndarray:
    if a.size == 0:
        return a
    idx = np.where(np.abs(a) > thr)[0]
    if idx.size == 0:
        return a[:0]
    keep = int(sr * keep_ms / 1000)
    return a[max(0, idx[0] - keep): min(a.size, idx[-1] + keep)]


def render_paragraph(engine: BaseEngine, text: str, text_opts: dict, params: dict,
                     stop_flag=None) -> np.ndarray:
    """Нормализует текст, режет на предложения, синтезирует, добавляет паузы."""
    norm = textproc.normalize_for_tts(text, text_opts, engine.caps())
    if not norm:
        return np.zeros(0, np.float32)
    if engine.stress_style != "none" and text_opts.get("smart_accent", True):
        from . import accent
        norm = accent.accent_text(norm)     # без скачанных моделей — текст как есть
    norm = textproc.apply_stress_style(norm, engine.stress_style)
    sent_pause = float(params.get("sentence_pause_ms", 0))
    para_pause = float(params.get("paragraph_pause_ms", 500))
    vol = float(params.get("volume", 1.0))
    trim = bool(params.get("trim_silence", True))
    if sent_pause > 0:
        chunks = textproc.chunk_text(norm, engine.max_chars)
        # мелкие предложения всё равно синтезируем по одному — ради пауз
    else:
        chunks = _pack(textproc.chunk_text(norm, engine.max_chars), engine.max_chars)
    pieces = []
    for i, ch in enumerate(chunks):
        if stop_flag is not None and stop_flag():
            break
        a = engine.synth(ch)
        if trim:
            a = trim_silence(a, sr=engine.sample_rate)
        pieces.append(a)
        if i < len(chunks) - 1:
            pieces.append(silence(sent_pause if sent_pause > 0 else 120, engine.sample_rate))
    pieces.append(silence(para_pause, engine.sample_rate))
    out = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
    if vol != 1.0:
        out = out * vol
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def _pack(chunks: list[str], max_chars: int) -> list[str]:
    out = []
    buf = ""
    for c in chunks:
        if buf and len(buf) + 1 + len(c) > max_chars:
            out.append(buf)
            buf = c
        else:
            buf = (buf + " " + c).strip()
    if buf:
        out.append(buf)
    return out


# ==========================================================================
# Скорость голоса и оценка длительности
# ==========================================================================
CALIBRATION_TEXT = (
    "В 1957 году был запущен первый искусственный спутник Земли. Его сигналы принимали "
    "радиолюбители всего мира, и событие стало по-настоящему международным. Через месяц "
    "полетел второй спутник, а на его борту находилась собака по кличке Лайка.")
DEFAULT_RATE = 15.0        # символов «как произносится» в секунду речи, скорость ×1


def rate_key(engine_id: str, voice_id: str, speed: float) -> str:
    return f"{engine_id}|{voice_id}|{round(float(speed) * 20) / 20:.2f}"


def rate_for(rates: dict, engine_id: str, voice_id: str, speed: float) -> tuple[float, bool]:
    """Скорость речи из замеров: (символов в секунду, точный ли замер).
    Нет замера при этой скорости — берём ближайший замер голоса и пересчитываем."""
    r = rates.get(rate_key(engine_id, voice_id, speed))
    if r:
        return r, True
    prefix = f"{engine_id}|{voice_id}|"
    known = [(abs(float(k[len(prefix):]) - speed), float(k[len(prefix):]), v)
             for k, v in rates.items() if k.startswith(prefix)]
    if known:
        _, sp, v = min(known)
        return v * speed / sp, False
    return DEFAULT_RATE * speed, False


def measure_voice_rate(engine_id: str, voice_id: str, params: dict, text_opts: dict) -> float:
    """Сколько символов (числа — словами) голос произносит за секунду при текущей
    скорости. Замеряем именно при ней: у Piper «×1.3» ускоряет речь не в 1.3 раза.
    Паузы не входят — их оценка добавляет отдельно."""
    p = dict(params)
    engine = create_engine(engine_id)
    try:
        engine.load(voice_id, p)
        norm = textproc.normalize_for_tts(CALIBRATION_TEXT, text_opts, engine.caps())
        spoken = _spoken_len(CALIBRATION_TEXT, text_opts, engine.caps())
        engine.synth("Проверка.")      # первый вызов медленный и бывает с щелчком — не считаем
        a = engine.synth(norm)
        if p.get("trim_silence", True):
            a = trim_silence(a, sr=engine.sample_rate)
        sec = a.size / float(engine.sample_rate or 1)
    finally:
        engine.close()
    if sec < 1:
        raise EngineError("голос вернул слишком короткий звук")
    return spoken / sec


def _spoken_len(text: str, text_opts: dict, caps: dict) -> int:
    """Длина текста «как произносится»: числа всегда словами — даже если движок
    читает цифры сам, «1961» звучит как «тысяча девятьсот шестьдесят один»."""
    return len(textproc.normalize_for_tts(text, {**text_opts, "numbers": "on"}, caps))


def estimate_seconds(texts: list[str], engine_id: str, rate: float | None, params: dict,
                     text_opts: dict, sample_chars: int = 60000) -> float:
    """Длительность озвучки так, как её соберёт render_paragraph: нормализованный текст
    (числа и сокращения словами — он заметно длиннее исходного) + паузы между кусками
    и абзацами. Для больших текстов нормализуется равномерная выборка абзацев."""
    if not texts:
        return 0.0
    cls = ENGINES.get(engine_id, BaseEngine)
    caps, max_chars = cls.caps(), cls.max_chars
    speed = max(0.3, float(params.get("speed", 1.0)))
    rate = rate or DEFAULT_RATE * speed     # rate — уже при текущей скорости
    sent_pause = float(params.get("sentence_pause_ms", 0))
    gap = (sent_pause if sent_pause > 0 else 120) / 1000.0
    para_pause = float(params.get("paragraph_pause_ms", 500)) / 1000.0
    total_raw = sum(len(t) for t in texts)
    # каждый step-й абзац: в выборку попадает ≈ sample_chars символов
    sample = texts[::max(1, round(total_raw / sample_chars))]
    s_raw = s_norm = s_gaps = 0
    for t in sample:
        norm = textproc.normalize_for_tts(t, text_opts, caps)
        s_raw += len(t)
        s_norm += _spoken_len(t, text_opts, caps)
        if norm:
            chunks = textproc.chunk_text(norm, max_chars)
            n = len(chunks) if sent_pause > 0 else len(_pack(chunks, max_chars))
            s_gaps += max(0, n - 1)
    scale = total_raw / s_raw if s_raw else 1.0
    return s_norm * scale / rate + s_gaps * scale * gap + len(texts) * para_pause
