"""Журнал работы программы — чтобы по отчёту пользователя понять, что у него сломалось.

data/logs/docvoice.log — события и ошибки с подробностями (до 5 файлов по 1 МБ);
data/logs/crash.log    — падения внутри библиотек на C (espeak, onnx, torch).

Личное в журнал не пишем: вместо пути к книге — расширение, размер и признак
«в имени есть не латинские буквы».
"""
from __future__ import annotations

import datetime
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import zipfile
from pathlib import Path

from . import __version__
from .paths import app_dir, data_dir

log = logging.getLogger("docvoice")
_crash_file = None


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def setup() -> None:
    """Включить журнал. Вызывается один раз при запуске, до всего остального."""
    global _crash_file
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    try:
        h = logging.handlers.RotatingFileHandler(logs_dir() / "docvoice.log", maxBytes=1_000_000,
                                                 backupCount=4, encoding="utf-8", delay=False)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
        log.addHandler(h)
    except OSError:
        log.addHandler(logging.NullHandler())
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        log.addHandler(sh)

    # падения нативного кода
    try:
        import faulthandler
        _crash_file = open(logs_dir() / "crash.log", "a", encoding="utf-8")
        _crash_file.write(f"\n=== запуск {datetime.datetime.now():%Y-%m-%d %H:%M:%S}, версия {__version__} ===\n")
        _crash_file.flush()
        faulthandler.enable(_crash_file)
    except Exception:
        pass

    # необработанные исключения в любом потоке
    def excepthook(tp, val, tb):
        log.critical("Необработанная ошибка", exc_info=(tp, val, tb))
        _notify_gui(val)

    def thread_hook(args):
        log.critical(f"Необработанная ошибка в потоке {args.thread.name if args.thread else '?'}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    log.info("=" * 70)
    log.info(f"DocVoice {__version__} запущен")
    for line in system_info():
        log.info("  " + line)


_gui_notifier = None


def set_gui_notifier(fn) -> None:
    """Окно программы может показать человеку понятное сообщение о сбое."""
    global _gui_notifier
    _gui_notifier = fn


def _notify_gui(exc) -> None:
    if _gui_notifier:
        try:
            _gui_notifier(exc)
        except Exception:
            pass


def install_qt_handler() -> None:
    """Предупреждения Qt (шрифты, плагины, звук) — тоже в журнал."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def handler(mode, context, message):
        log.log(levels.get(mode, logging.WARNING), f"Qt: {message}")

    qInstallMessageHandler(handler)


# --------------------------------------------------------------------------
# сведения о компьютере
# --------------------------------------------------------------------------
def _ram_gb() -> tuple[float, float] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 2 ** 30, m.ullAvailPhys / 2 ** 30
    except Exception:
        return None


def ram_gb() -> tuple[float, float] | None:
    """(всего, свободно) ГБ оперативной памяти."""
    return _ram_gb()


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "нет"


def _ascii_flag(p) -> str:
    return "латиница" if str(p).isascii() else "ЕСТЬ не латинские буквы"


def _data_dir_note() -> str:
    if os.environ.get("DOCVOICE_DATA"):
        return " (задана переменной DOCVOICE_DATA)"
    if data_dir() != app_dir() / "data":
        return " (в профиле пользователя: рядом с программой писать нельзя)"
    return " (рядом с программой)"


def system_info() -> list[str]:
    out = []
    try:
        out.append(f"Windows: {platform.platform()} ({platform.version()}), {platform.machine()}")
    except Exception:
        pass
    out.append(f"Python {platform.python_version()}, {'exe-сборка' if getattr(sys, 'frozen', False) else 'из исходников'}")
    if sys.platform == "win32":
        try:
            import ctypes
            out.append(f"Кодовая страница ANSI: {ctypes.windll.kernel32.GetACP()}, "
                       f"язык: {ctypes.windll.kernel32.GetUserDefaultUILanguage():#06x}")
        except Exception:
            pass
    out.append(f"Папка программы: {_ascii_flag(app_dir())}; папка данных: {_ascii_flag(data_dir())}"
               f"{_data_dir_note()}")
    out.append(f"Профиль пользователя: {_ascii_flag(Path.home())}")
    out.append(f"Процессор: {os.cpu_count()} потоков")
    r = _ram_gb()
    if r:
        out.append(f"Память: {r[0]:.1f} ГБ, свободно {r[1]:.1f} ГБ")
    try:
        import shutil
        out.append(f"Свободно на диске с данными: {shutil.disk_usage(data_dir()).free / 2 ** 30:.1f} ГБ")
    except Exception:
        pass
    pk = ", ".join(f"{n} {_pkg_version(n)}" for n in
                   ("PySide6", "numpy", "soundfile", "sounddevice", "onnxruntime", "piper-tts",
                    "torch", "pymupdf", "tokenizers"))
    out.append(f"Библиотеки: {pk}")
    return out


def describe_file(path) -> str:
    """Безличное описание файла книги для журнала."""
    p = Path(path)
    try:
        size = f"{p.stat().st_size / 1e6:.1f} МБ"
    except OSError:
        size = "размер неизвестен"
    return f"{p.suffix.lower() or 'без расширения'}, {size}, имя: {_ascii_flag(p.name)}, путь: {_ascii_flag(p.parent)}"


# --------------------------------------------------------------------------
# отчёт о проблеме
# --------------------------------------------------------------------------
_PRIVATE_KEYS = {"recent", "last_dir", "export_dir", "geometry", "splitter", "test_phrase", "ai"}


def _safe_settings() -> dict:
    try:
        s = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
    except Exception as e:
        return {"ошибка чтения настроек": str(e)}
    for k in _PRIVATE_KEYS:
        if k in s:
            s[k] = "(скрыто)"
    # слова из словаря и правил могут быть из книги — оставляем только количество
    t = s.get("text", {})
    if isinstance(t, dict) and "replacements" in t:
        t["replacements"] = f"(замен: {len(t['replacements'])})"
    f = s.get("filter", {})
    if isinstance(f, dict) and "rules" in f:
        f["rules"] = f"(правил: {len(f['rules'])})"
    if "filter_profiles" in s:
        s["filter_profiles"] = f"(профилей: {len(s['filter_profiles'])})"
    return s


def make_report(dest_dir: Path | None = None) -> Path:
    """ZIP с журналами, сведениями о компьютере и настройками без личного.
    Кладётся на рабочий стол (или в dest_dir)."""
    if dest_dir is None:
        dest_dir = Path.home() / "Desktop"
        if not dest_dir.is_dir():
            dest_dir = data_dir()
    dest = Path(dest_dir) / f"DocVoice-отчёт-{datetime.datetime.now():%Y-%m-%d_%H-%M}.zip"
    log.info(f"Сохраняю отчёт о проблеме")
    for h in log.handlers:
        h.flush()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(logs_dir().glob("*.log*")):
            z.write(f, f"logs/{f.name}")
        z.writestr("компьютер.txt", f"DocVoice {__version__}\n" + "\n".join(system_info()))
        z.writestr("настройки.json", json.dumps(_safe_settings(), ensure_ascii=False, indent=2))
        z.writestr("голоса.txt", "\n".join(_voices_list()))
    return dest


def _voices_list() -> list[str]:
    out = []
    try:
        from .engines import ENGINES
        for eid, cls in ENGINES.items():
            ok, msg = cls.available()
            try:
                voices = [v.name for v in cls.list_voices()] if ok else []
            except Exception as e:
                voices = [f"ошибка списка: {e}"]
            out.append(f"{cls.title}: {'доступен' if ok else 'НЕДОСТУПЕН — ' + msg}; голосов: {len(voices)}")
            out += [f"    {v}" for v in voices[:40]]
    except Exception as e:
        out.append(f"ошибка: {e}")
    try:
        from . import accent
        out.append(f"Умные ударения: {'скачаны' if accent.installed() else 'не скачаны'}"
                   + (f", ошибка загрузки: {accent.last_error()}" if accent.last_error() else ""))
    except Exception:
        pass
    return out
