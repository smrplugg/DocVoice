"""«Связь с разработчиком»: комментарий + журнал уходят письмом на почту разработчика.

Письмо отправляет скрипт Google Apps Script в аккаунте разработчика (support/apps_script.gs):
программа шлёт ему JSON, он пересылает всё на почту. Пароля от почты в программе нет.
Если адрес скрипта не задан или нет интернета — запасной путь: архив на рабочий стол
и письмо через почтовую программу человека (mailto).
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .log import log, logs_dir, make_report, system_info

DEVELOPER_EMAIL = "smrplugg@gmail.com"
# Адрес веб-приложения Apps Script и ключ (см. support/КАК-НАСТРОИТЬ.txt) хранятся
# в docvoice/support_config.py — он не попадает в репозиторий (образец: support_config.example.py).
# Можно задать и переменными окружения DOCVOICE_SUPPORT_URL / DOCVOICE_SUPPORT_TOKEN.
# Пусто — работает только запасной путь (архив на рабочий стол + mailto).
try:
    from .support_config import SUPPORT_URL, SUPPORT_TOKEN  # type: ignore
except ImportError:
    import os as _os
    SUPPORT_URL = _os.environ.get("DOCVOICE_SUPPORT_URL", "")
    SUPPORT_TOKEN = _os.environ.get("DOCVOICE_SUPPORT_TOKEN", "")


def log_tail(lines: int = 300) -> str:
    """Последние строки журнала — показываются человеку перед отправкой."""
    for h in log.handlers:
        try:
            h.flush()
        except Exception:
            pass
    p = logs_dir() / "docvoice.log"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def configured() -> bool:
    return SUPPORT_URL.startswith("https://")


def send(comment: str, contact: str = "", attach_log: bool = True, timeout: int = 60) -> None:
    """Отправить сообщение разработчику. Ошибка — исключение с понятным текстом."""
    if not configured():
        raise RuntimeError("отправка через интернет не настроена")
    payload = {
        "token": SUPPORT_TOKEN,
        "version": __version__,
        "subject": " ".join(comment.split())[:60] or "сообщение",
        "comment": comment,
        "contact": contact,
        "system": "\n".join(system_info()),
        "log_tail": log_tail(150) if attach_log else "",
    }
    if attach_log:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            z = make_report(Path(td))
            payload["zip"] = base64.b64encode(z.read_bytes()).decode("ascii")
            payload["zip_name"] = "docvoice-report.zip"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(SUPPORT_URL, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": f"DocVoice/{__version__}"})
    log.info(f"Отправляю сообщение разработчику ({len(data) // 1024} КБ)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
    except OSError as e:
        log.warning(f"Сообщение не отправлено: {e}")
        raise RuntimeError(f"нет связи с сервером ({e})") from e
    try:
        ans = json.loads(body)
    except ValueError:
        ans = {"ok": False, "message": body[:200]}
    if not ans.get("ok"):
        log.warning(f"Сервер не принял сообщение: {ans.get('message')}")
        raise RuntimeError(f"сервер не принял сообщение ({ans.get('message')})")
    log.info("Сообщение разработчику отправлено")


def mailto_url(comment: str, contact: str = "") -> str:
    """Письмо для почтовой программы: вложения mailto не умеет, поэтому просим приложить
    архив вручную, а в текст кладём сведения о компьютере (они короткие)."""
    body = (f"{comment}\n\nКак связаться: {contact or 'ответьте на это письмо'}\n\n"
            f"(Приложите, пожалуйста, архив DocVoice-отчёт с рабочего стола.)\n\n"
            f"--- DocVoice {__version__} ---\n" + "\n".join(system_info()))
    q = urllib.parse.urlencode({"subject": f"DocVoice {__version__}: сообщение", "body": body[:1800]},
                               quote_via=urllib.parse.quote)
    return f"mailto:{DEVELOPER_EMAIL}?{q}"
