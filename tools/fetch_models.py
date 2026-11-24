"""Скачивает всё, что нужно программе «из коробки», в указанную папку data.

Используется при сборке на GitHub (см. .github/workflows/build.yml), чтобы установщик
работал сразу — без кнопки «Голоса» и без интернета у пользователя.
Качает тем же кодом, что и сама программа, поэтому раскладка файлов совпадает.

    python tools/fetch_models.py <папка_data> [--mirror https://huggingface.co]

Уже скачанные файлы пропускаются (на GitHub папка кэшируется между сборками).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PIPER_VOICES = [
    "ru_RU-irina-medium",
    "ru_RU-denis-medium",
    "ru_RU-dmitri-medium",
    "ru_RU-ruslan-medium",
]
SILERO_MODEL = "v5_6_ru"
# Что выбрано при первом запуске (дальше человек может поменять — его настройки не трогаются)
STARTER_SETTINGS = {
    "engine": "silero",
    "voices": {
        "silero": f"{SILERO_MODEL}|xenia",
        "piper": "ru_RU-irina-medium.onnx",
    },
}


def retry(fn, what: str, tries: int = 4):
    for n in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # сеть на раннерах иногда моргает
            if n == tries:
                raise
            print(f"  ! {what}: {e} — повтор через {5 * n} с", flush=True)
            time.sleep(5 * n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("--mirror", default="https://huggingface.co")
    args = ap.parse_args()

    data = Path(args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    # Все пути программы (piper_dir, silero_dir, accent_dir) пойдут в эту папку
    os.environ["DOCVOICE_DATA"] = str(data)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from docvoice.audio import download
    from docvoice.engines import SILERO_MODELS
    from docvoice.paths import piper_dir, silero_dir
    from docvoice import accent

    mirror = args.mirror.rstrip("/")

    print("== Голоса Piper", flush=True)
    for key in PIPER_VOICES:
        code, name, quality = key.split("-", 2)
        rel = f"{code.split('_')[0]}/{code}/{name}/{quality}/{key}"
        for ext in (".onnx.json", ".onnx"):
            dest = piper_dir() / f"{key}{ext}"
            if dest.is_file() and dest.stat().st_size > 0:
                print(f"  = {dest.name}")
                continue
            url = f"{mirror}/rhasspy/piper-voices/resolve/main/{rel}{ext}?download=true"
            retry(lambda: download(url, dest, timeout=60), dest.name)
            print(f"  + {dest.name} ({dest.stat().st_size // 2**20} МБ)", flush=True)
    vj = piper_dir() / "voices.json"
    if not vj.is_file():
        retry(lambda: download(f"{mirror}/rhasspy/piper-voices/resolve/main/voices.json?download=true",
                               vj, timeout=60), "voices.json")

    print("== Silero", flush=True)
    dest = silero_dir() / f"{SILERO_MODEL}.pt"
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"  = {dest.name}")
    else:
        url = SILERO_MODELS[SILERO_MODEL][0]
        retry(lambda: download(url, dest, timeout=120), dest.name)
        print(f"  + {dest.name} ({dest.stat().st_size // 2**20} МБ)", flush=True)

    print("== Умные ударения", flush=True)
    if accent.installed() and (accent.accent_dir() / "dictionary" / "accents.sqlite").is_file():
        print("  = уже скачаны")
    else:
        retry(lambda: accent.download_models(mirror), "модели ударений")
        print("  + готово", flush=True)

    # стартовые настройки кладём отдельным файлом: установщик положит его как settings.json,
    # только если у человека настроек ещё нет
    (data / "settings.default.json").write_text(
        json.dumps(STARTER_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")

    for junk in list(data.rglob("*.part")) + list(data.rglob("*.tmp")):
        junk.unlink(missing_ok=True)
    total = sum(p.stat().st_size for p in data.rglob("*") if p.is_file())
    print(f"Итого в {data}: {total / 2**30:.2f} ГБ", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
