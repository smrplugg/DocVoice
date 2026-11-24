import os
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Папка с моделями и настройками. По умолчанию — рядом с программой (portable),
    если туда нельзя писать — в %LOCALAPPDATA%\\DocVoice."""
    env = os.environ.get("DOCVOICE_DATA")
    if env:
        p = Path(env)
    else:
        p = app_dir() / "data"
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".write_test"
            test.write_text("ok")
            test.unlink()
        except OSError:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
            p = Path(base) / "DocVoice"
    p.mkdir(parents=True, exist_ok=True)
    return p


def piper_dir() -> Path:
    p = data_dir() / "piper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def silero_dir() -> Path:
    p = data_dir() / "silero"
    p.mkdir(parents=True, exist_ok=True)
    return p
