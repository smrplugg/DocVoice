"""«Умные ударения»: ударения и буква «ё» по смыслу предложения (модели RUAccent).

Модели (~525 МБ) скачиваются по желанию в data/accent. Пока они не скачаны или не
загрузились, accent_text() возвращает текст как есть — чтение работает и без них.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from ..log import log
from ..paths import data_dir

REPO = "ruaccent/accentuator"
# (путь в репозитории, размер в байтах — для полосы загрузки)
MODEL_FILES = [
    ("dictionary/accents.json.gz", 20_950_000),
    ("dictionary/omographs.json.gz", 220_000),
    ("dictionary/yo_homographs.json.gz", 10_000),
    ("dictionary/yo_words.json.gz", 550_000),
    ("nn/nn_accent/config.json", 1_000),
    ("nn/nn_accent/model.onnx", 800_000),
    ("nn/nn_accent/vocab.txt", 1_000),
    ("nn/nn_accent/special_tokens_map.json", 200),
    ("nn/nn_stress_usage_predictor/config.json", 1_000),
    ("nn/nn_stress_usage_predictor/model.onnx", 116_470_000),
    ("nn/nn_stress_usage_predictor/tokenizer.json", 2_410_000),
    ("nn/nn_stress_usage_predictor/special_tokens_map.json", 200),
    ("nn/nn_yo_homograph_resolver/config.json", 1_000),
    ("nn/nn_yo_homograph_resolver/model.onnx", 14_330_000),
    ("nn/nn_yo_homograph_resolver/tokenizer.json", 130_000),
    ("nn/nn_yo_homograph_resolver/special_tokens_map.json", 200),
    ("nn/nn_omograph/turbo3.1/config.json", 1_000),
    ("nn/nn_omograph/turbo3.1/model.onnx", 359_310_000),
    ("nn/nn_omograph/turbo3.1/tokenizer.json", 5_530_000),
    ("nn/nn_omograph/turbo3.1/special_tokens_map.json", 200),
]
TOTAL_BYTES = sum(s for _, s in MODEL_FILES)

# слово, где ударение уже поставлено вручную («з+амок» или знак над буквой), не трогаем
MANUAL_STRESS = r"\S*(?:\+|́)\S*"

_lock = threading.Lock()
_model = None
_error = ""


def accent_dir() -> Path:
    return data_dir() / "accent"


def installed() -> bool:
    d = accent_dir()
    return all((d / rel).is_file() for rel, _ in MODEL_FILES)


def installed_size() -> int:
    d = accent_dir()
    return sum((d / rel).stat().st_size for rel, _ in MODEL_FILES if (d / rel).is_file())


def download_models(mirror: str, progress=None, stop_flag=None):
    """Скачивает недостающие файлы. progress(подпись, скачано байт, всего байт)."""
    from ..audio import download
    mirror = (mirror or "https://huggingface.co").rstrip("/")
    d = accent_dir()
    done = sum(s for rel, s in MODEL_FILES if (d / rel).is_file())
    for rel, size in MODEL_FILES:
        dest = d / rel
        if dest.is_file():
            continue
        base = done

        def prog(got, total, base=base):
            if progress:
                progress("Умные ударения", base + got, TOTAL_BYTES)

        download(f"{mirror}/{REPO}/resolve/main/{rel}", dest, prog, stop_flag, timeout=60)
        done += size
    if progress:
        progress("Готовлю словарь ударений (около минуты)", TOTAL_BYTES, TOTAL_BYTES)
    from ._ruaccent import build_accent_db
    build_accent_db(str(d))
    unload()


def delete_models():
    unload()
    shutil.rmtree(accent_dir(), ignore_errors=True)


def unload():
    global _model
    with _lock:
        _model = None


def last_error() -> str:
    return _error


def get():
    """Загруженная модель или None. Первая загрузка — около 10 секунд."""
    global _model, _error
    with _lock:
        if _model is None and installed():
            try:
                from ._ruaccent import Accentizer
                t = time.time()
                m = Accentizer()
                m.load(str(accent_dir()))
                _model, _error = m, ""
                log.info(f"Умные ударения загружены за {time.time() - t:.1f} с")
            except Exception as e:     # битые или неполные файлы — читаем без ударений
                _error = f"{type(e).__name__}: {e}"
                log.exception("Умные ударения не загрузились — читаю без них")
        return _model


def is_loaded() -> bool:
    return _model is not None


def preload_async():
    """Загрузить модели в фоне, чтобы первое чтение не ждало 10 секунд."""
    if installed() and _model is None:
        threading.Thread(target=get, daemon=True).start()


def accent_text(text: str) -> str:
    """Ставит «+» перед ударными гласными и букву «ё». Без моделей — текст как есть."""
    if not text or not any("а" <= c.lower() <= "я" or c in "ёЁ" for c in text):
        return text
    m = get()
    if m is None:
        return text
    text = text.replace("–", "—")     # иначе модель выбросит короткое тире вместе с паузой
    try:
        with _lock:
            return m.process(text, skip_regex=MANUAL_STRESS)
    except Exception:
        return text
