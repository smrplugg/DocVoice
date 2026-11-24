"""Хранение настроек в data/settings.json."""
from __future__ import annotations

import copy
import datetime
import json

from .log import log
from .paths import data_dir
from .textproc import DEFAULT_FILTER

DEFAULTS = {
    "engine": "piper",
    "voices": {},                    # engine_id -> voice_id
    "params": {
        "speed": 1.0,
        "pitch": 0.0,
        "volume": 1.0,
        "sentence_pause_ms": 0,
        "paragraph_pause_ms": 500,
        "trim_silence": True,
        "piper_noise_scale": None,
        "piper_noise_w": None,
        "silero_sample_rate": 48000,
        "silero_accent": True,
        "silero_yo": True,
        "silero_threads": 0,
    },
    "text": {
        "join_lines": True,
        "fix_hyphen": True,
        "merge_broken": True,
        "max_para_chars": 2500,
        "numbers": "auto",           # auto | on | off
        "translit": "auto",          # auto | on | off
        "abbreviations": True,
        "symbols": True,
        "replacements": [],
        "smart_accent": True,        # ударения и «ё» по смыслу (если скачаны модели)
    },
    "filter": copy.deepcopy(DEFAULT_FILTER),
    "filter_profiles": {},
    "export": {"format": "mp3", "quality": "Высокое", "split_mode": None, "split_minutes": 30,
               "artist": ""},
    "advanced": False,               # показывать все настройки (иначе — простой режим)
    "ui_scale": 125,                 # размер надписей, %
    "voice_rates": {},               # замеры скорости речи: engine|voice|speed -> символов/с
    "synth_rates": {},               # скорость синтеза при записи: engine|voice -> символов/с
    "follow_reading": True,
    "show_excluded": True,
    "output_device": None,
    "piper_mirror": "https://huggingface.co",
    "piper_lang_filter": "ru",
    "last_dir": "",
    "recent": [],
    "geometry": None,
    "splitter": None,
    "ai": {"url": "https://api.numes.ai/api/v1", "key": "", "model": "deepseek/deepseek-v4.1-flash"},
    "test_phrase": "Привет! Это проверка голоса. Съешь же ещё этих мягких французских булок, да выпей чаю.",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("voices", "filter_profiles"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self):
        self.path = data_dir() / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        try:
            if self.path.exists():
                self.data = _merge(DEFAULTS, json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            # испорченный файл (например, компьютер выключили во время записи):
            # сохраняем его копию для разбора и начинаем с настроек по умолчанию
            log.exception("Настройки не прочитались — начинаю с настроек по умолчанию")
            try:
                self.path.replace(self.path.with_name(f"settings.broken-{datetime.date.today()}.json"))
            except OSError:
                pass

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("Не удалось сохранить настройки")

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
