"""Помощник на нейросети: команды словами → правила очистки, записи словаря произношения,
включение готовых фильтров, разметка и названия глав, пропуск кусков книги, правка отдельных
строк, настройки программы или просто ответ. Перед ответом нейросеть может сама посмотреть
книгу инструментами (поиск, строки по номерам, проверка правила) — см. BookTools; разговор
помнит прошлые реплики. Отдельно — проход по всей книге за словарём произношения.

Нейросеть сама текст книги не меняет: она только предлагает изменения, программа проверяет
их, показывает, что они заденут, и применяет после подтверждения. Работает с любым сервером
в формате OpenAI Chat Completions (по умолчанию — Numes;
запасные — Gemini и Pollinations). Ключ хранится только в настройках
пользователя и в журналы/отчёты не попадает.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import __version__, textproc
from .log import log

DEFAULT_URL = "https://api.numes.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_CONTEXT_CHARS = 9000      # остальное нейросеть найдёт инструментами
MAX_HEADINGS_CHARS = 5000

# Запасные нейросети: если основная не ответила (лимит, сбой, нет денег), пробуем их по порядку.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
POLLINATIONS_URL = "https://text.pollinations.ai/openai"
DEFAULT_FALLBACKS = [
    # бесплатный ключ — в Google AI Studio; без ключа пропускается
    {"name": "Gemini", "url": GEMINI_URL, "key": "", "model": "gemini-flash-latest", "enabled": True},
    # без ключа и без регистрации, но модель слабее (GPT-OSS 20B) и не чаще раза в 15 с
    {"name": "Pollinations", "url": POLLINATIONS_URL, "key": "", "model": "openai-fast", "enabled": True,
     "keyless": True},
]

# готовые фильтры, которые помощник может включить/выключить (ключ → описание для людей)
OPTIONS = {
    "page_numbers": "строки — номера страниц",
    "url_lines": "строки — ссылки и e-mail",
    "url_inline": "ссылки и e-mail внутри текста",
    "no_letters": "строки без букв",
    "footnote_marks": "знаки сносок [12], ¹²³, *",
    "footnote_numbers": "приклеенные номера сносок по порядку",
    "footnote_lines": "абзацы-сноски «18 Кол. 2:3» внутри текста",
    "toc_lines": "строки оглавления",
    "square_brackets": "всё в [квадратных скобках]",
    "parentheses": "всё в (круглых скобках)",
}

# Инструкция для нейросети. <<OPTIONS>> подставляется в run(); фигурные скобки — как есть (не .format).
SYSTEM = """Ты — помощник в программе DocVoice, которая читает книги вслух синтезатором речи.
Человек пишет просьбу словами. Ты НЕ правишь книгу сам, а предлагаешь действия: программа их
проверит, покажет человеку, и он применит то, что одобрит. Это разговор: учитывай прошлые просьбы,
свои прошлые ответы и то, что человек применил или отклонил.

ИНСТРУМЕНТЫ — чтобы посмотреть книгу перед ответом.
Тебе прислана только выборка строк. Прежде чем предлагать правила, главы, пропуски или правку
строк, проверь книгу инструментами. Чтобы вызвать их, ответь JSON:
{"tools": [{"name": "search", "args": {"pattern": "Там же"}}, {"name": "lines", "args": {"from": 10, "to": 30}}]}
(до 5 вызовов за раз). Программа пришлёт результаты, и ты продолжишь.
- search: {"pattern": текст, "regex": false, "limit": 30} — строки, где встречается текст (без учёта
  регистра и разницы е/ё) или регулярное выражение Python ("regex": true); сколько всего + примеры.
- lines: {"from": N, "to": M} — строки книги подряд с номерами (не больше 60 за раз).
- test_rule: поля как у действия "rule" — сколько строк заденет правило и какие. Проверяй так
  каждое правило, прежде чем его предложить, и уточняй, если задевает лишнее или ничего.
- headings: {} — строки по всей книге, похожие на заголовки.
- words: {"kind": "latin" | "names" | "abbr", "limit": 60} — самые частые слова латиницей, имена и
  названия (слова с большой буквы не в начале предложения) или аббревиатуры — для словаря произношения.
Обычно хватает 1–3 шагов. Для настроек инструменты не нужны. Если нужного места нет в
присланной выборке — ОБЯЗАТЕЛЬНО посмотри инструментом, а не угадывай, что написано в строке.
Программа проверит твой окончательный ответ и, если правило ничего не находит или номер строки
не тот, вернёт замечания — тогда исправь.

ДЕЙСТВИЯ — окончательный ответ.
1. "rule" — правило очистки текста. Поля:
   "kind": "contains" (строка содержит текст) | "exact" (строка совпадает целиком) |
           "startswith" (строка начинается с) | "template" (как образец, цифры в нём любые) |
           "regex" (регулярное выражение Python, re.IGNORECASE);
   "pattern": текст или выражение (спецсимволы вроде \d работают ТОЛЬКО в "regex");
   "action": "line" — не читать всю строку | "cut" — вырезать только совпадение
             (для "cut" годятся только "contains" и "regex");
   "where": "any" | "edges" (только первые/последние строки страницы — колонтитулы);
   "case": true, если важен регистр букв.
   Правило применяется к КАЖДОЙ СТРОКЕ документа отдельно. Делай его как можно точнее.
2. "replace" — запись словаря произношения: "find" — слово или фраза в тексте,
   "replace" — как читать. Ударение — знак + перед ударной гласной (Г+ёте, з+амок),
   иностранное — русскими буквами (NASA → Н+АСА). Ищется целое слово без учёта регистра.
   "regex": true — если "find" регулярное выражение (тогда в "replace" можно \\1).
   Если слово склоняется, сделай отдельные записи на нужные формы или regex.
   Обычные русские слова не добавляй — их ударения программа ставит сама по словарю.
3. "option" — включить/выключить готовый фильтр: "key" — один из <<OPTIONS>>; "value": true/false.
4. "chapters" — разметить главы заново (по ним книга делится на файлы при сохранении).
   Либо "pattern" — регулярное выражение Python (re.IGNORECASE) для строк-заголовков глав;
   программа применит его к началам абзацев по ВСЕЙ книге. Либо "lines" — список номеров строк-
   заголовков, если общего образца нет. Глава — крупная часть книги, а не каждый подзаголовок.
5. "delete" — не читать кусок книги: "from" — номер первой строки, "to" — номер последней
   (включительно; -1 — до конца книги). Для «убери предисловие», «не читай главу 3»,
   «удали примечания в конце». Кусок кончается перед заголовком следующей главы.
   Чтобы убрать слово или фразу по всей книге, используй "rule" с "action": "cut".
6. "edit" — исправить текст одной строки: "line" — номер, "find" — точный кусок этой строки,
   "replace" — на что заменить. Для ошибок распознавания, слипшихся или разорванных слов, мусорных
   символов в отдельных строках. Авторский текст не переписывай и смысл не меняй. Если ошибка
   повторяется по всей книге — лучше "rule" ("cut") или "replace".
7. "rename_chapter" — "line" — строка начала главы, "title" — новое название (так будет назван файл).
8. "setting" — изменить настройку программы: "key", "value". Ключи, текущие значения и допустимые
   варианты — в разделе «Настройки» ниже. Голос: "key": "voice", "value": id голоса, "engine": id движка.
   Меняй умеренно, от текущего значения: «помедленнее»/«побыстрее» — примерно на 10–15%,
   «намного» — на 25–30%; паузы — на 200–500 мс. Крайние значения — только если попросили прямо.

Ответь ТОЛЬКО JSON-объектом без пояснений вокруг — либо {"tools": [...]}, либо окончательный:
{"reply": "коротко по-русски, что ты предлагаешь и почему (или ответ на вопрос)",
 "actions": [{"type": "rule", ..., "why": "зачем"}, ...]}
Если просьба — вопрос без действий, верни "actions": []. Если не хватает сведений — спроси в "reply".
Не выдумывай строки и номера: опирайся на присланный текст и результаты инструментов."""


class AIError(Exception):
    """Ошибка с понятным человеку текстом."""


class TransientError(AIError):
    """Сбой у поставщика или по дороге — имеет смысл повторить запрос."""


@dataclass
class Proposal:
    data: dict
    title: str                 # что сделать — для человека
    detail: str = ""           # что заденет
    ok: bool = True            # прошло проверку
    checked: bool = True
    samples: list[str] = field(default_factory=list)


@dataclass
class Answer:
    reply: str
    proposals: list[Proposal]
    seconds: float
    model: str
    via: str = ""              # какая нейросеть ответила, если не основная, и почему
    memory: str = ""           # окончательный ответ нейросети — для истории разговора
    looked: list[str] = field(default_factory=list)   # что помощник смотрел инструментами
    requests: int = 1          # сколько запросов ушло к нейросети


def config(settings) -> dict:
    a = settings.data.setdefault("ai", {})
    a.setdefault("url", DEFAULT_URL)
    a.setdefault("key", "")
    a.setdefault("model", DEFAULT_MODEL)
    fb = a.get("fallbacks")
    if not isinstance(fb, list):
        fb = a["fallbacks"] = []
    have = {f.get("name") for f in fb if isinstance(f, dict)}
    for d in DEFAULT_FALLBACKS:
        if d["name"] not in have:
            fb.append(dict(d))
    return a


def providers(cfg: dict) -> list[dict]:
    """Нейросети по порядку: основная (если есть ключ), затем включённые запасные."""
    out = []
    if (cfg.get("key") or "").strip():
        out.append({"name": "основная", "url": cfg.get("url") or DEFAULT_URL, "key": cfg["key"],
                    "model": cfg.get("model") or DEFAULT_MODEL})
    for f in cfg.get("fallbacks") or []:
        if isinstance(f, dict) and f.get("enabled") and ((f.get("key") or "").strip() or f.get("keyless")):
            out.append(f)
    return out


def enabled(settings) -> bool:
    """Помощник показываем, только если человек сам ввёл хоть один ключ: одной
    бесплатной Pollinations для этого мало — иначе вкладка появилась бы у всех."""
    cfg = config(settings)
    return bool((cfg.get("key") or "").strip()) or any(
        isinstance(f, dict) and f.get("enabled") and (f.get("key") or "").strip()
        for f in cfg.get("fallbacks") or [])


def chat(cfg: dict, messages: list[dict], timeout: int = 600, max_tokens: int = 8000,
         stream: bool = True) -> tuple[str, str]:
    """Один запрос. Возвращает (текст ответа, модель)."""
    url = cfg.get("url", DEFAULT_URL).rstrip("/") + "/chat/completions"
    # stream: ответ идёт по кусочкам — иначе «думающая» модель молчит дольше 100 с
    # и промежуточный сервер (Cloudflare) обрывает соединение ошибкой 524
    body = {"model": cfg.get("model") or DEFAULT_MODEL, "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "stream": stream}
    if not (cfg.get("key") or "").strip().isascii():
        raise AIError("В ключе есть русские буквы или другие посторонние символы — "
                      "скопируйте его заново.")
    if "numes" in url or "openrouter" in url:
        body["provider"] = {"data_collection": "deny"}   # другие серверы на чужое поле ругаются
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST",
                                 headers={"Authorization": f"Bearer {(cfg.get('key') or '').strip()}",
                                          "Content-Type": "application/json; charset=utf-8",
                                          # стандартную подпись urllib сервер отклоняет (Cloudflare 1010)
                                          "User-Agent": f"DocVoice/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                d = _read_stream(r)
            else:
                d = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read()[:500].decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            msg = raw.strip()[:200]
        log.warning(f"Нейросеть: HTTP {e.code} {msg}")
        if e.code == 401:
            raise AIError("Ключ не подошёл. Проверьте его в настройках нейросети.") from None
        if e.code == 402:
            raise AIError("Сервер просит оплату: эта модель платная или на счёте нет денег. "
                          "Выберите бесплатную модель.") from None
        if e.code == 429:
            wait = e.headers.get("Retry-After")
            when = f" примерно через {max(1, round(int(wait) / 60))} мин." if wait and wait.isdigit() else " позже."
            raise AIError("Слишком много запросов к бесплатной модели. Попробуйте" + when) from None
        if e.code in (500, 502, 503, 504, 520, 522, 524):
            raise TransientError(f"Сервер нейросети не ответил вовремя (ошибка {e.code}). "
                                 "Попробуйте ещё раз.") from None
        if e.code == 404:
            raise AIError(f"Сервер не знает такую модель или адрес: {msg}") from None
        raise AIError(f"Сервер ответил ошибкой {e.code}: {msg}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning(f"Нейросеть: нет связи: {e}")
        raise AIError("Нет связи с сервером нейросети. Проверьте интернет.") from None
    try:
        msg = d["choices"][0]["message"]
        text = msg.get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise AIError("Сервер прислал непонятный ответ.") from None
    if not text.strip() and msg.get("tool_calls"):
        # модели, обученные на встроенных инструментах (GPT-OSS и др.), шлют вызов не текстом,
        # а в поле tool_calls — переводим в наш JSON
        text = _tool_calls_to_text(msg["tool_calls"])
    if not text.strip():
        if stream and d.get("reasoning_only"):
            # некоторые серверы (Pollinations) режут поток раньше, чем модель додумает;
            # без потока тот же запрос приходит целиком
            log.info("Нейросеть: в потоке одни размышления — повторяю без потока")
            return chat(cfg, messages, timeout=timeout, max_tokens=max_tokens, stream=False)
        # бывает при лимите или когда «думающая» модель израсходовала ответ на размышления
        raise TransientError("Нейросеть прислала пустой ответ. Попробуйте ещё раз.")
    return text, d.get("model") or body["model"]


def chat_retry(cfg: dict, messages: list[dict], tries: int = 3, **kw) -> tuple[str, str]:
    """chat() с повтором при сбоях поставщика (но не при лимите 429 — там ждать минуты)."""
    for i in range(tries):
        try:
            return chat(cfg, messages, **kw)
        except TransientError:
            if i == tries - 1:
                raise
            log.info(f"Нейросеть: сбой у поставщика, повторяю ({i + 2} из {tries})")
            time.sleep(3 * (i + 1))


def chat_any(cfg: dict, messages: list[dict], **kw) -> tuple[str, str, str]:
    """Пробует основную нейросеть, затем запасные. Возвращает (ответ, модель, пояснение —
    пусто, если ответила основная). Если не ответил никто — AIError со всеми причинами."""
    provs = providers(cfg)
    if not provs:
        raise AIError("Не задан ни один ключ нейросети. Откройте «Настройки…» помощника.")
    fails = []
    for i, pr in enumerate(provs):
        try:
            text, model = chat_retry(pr, messages, **kw)
        except AIError as e:
            log.warning(f"Нейросеть «{pr.get('name')}» не ответила: {e}")
            fails.append(f"{pr.get('name')}: {e}")
            continue
        via = ""
        if i:
            via = (f"Ответила запасная нейросеть «{pr.get('name')}» ({model}), потому что "
                   + "; ".join(fails))
        return text, model, via
    raise AIError("Ни одна нейросеть не ответила.\n" + "\n".join("• " + f for f in fails))


def _read_stream(r) -> dict:
    """Собрать ответ из Server-Sent Events в обычный вид {"choices": [{"message": …}], "model": …}."""
    content, model = [], ""
    calls: dict[int, dict] = {}
    reasoning = False
    for raw in r:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if ev.get("error"):
            msg = ev["error"].get("message") if isinstance(ev["error"], dict) else ev["error"]
            log.warning(f"Нейросеть: ошибка в потоке: {msg}")
            raise TransientError(f"Сервер прервал ответ ({msg}). Попробуйте ещё раз.")
        model = ev.get("model") or model
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if piece:
                content.append(piece)
            if delta.get("reasoning") or delta.get("reasoning_content"):
                reasoning = True
            for tc in delta.get("tool_calls") or []:      # вызов инструмента приходит кусочками
                slot = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
    tool_calls = [{"function": calls[k]} for k in sorted(calls)]
    return {"choices": [{"message": {"content": "".join(content), "tool_calls": tool_calls}}], "model": model,
            "reasoning_only": reasoning and not content and not tool_calls}


def _tool_calls_to_text(calls: list) -> str:
    """Встроенные вызовы инструментов → наш JSON {"tools": [...]} (или окончательный ответ,
    если модель «вызвала» его функцией)."""
    tools = []
    for c in calls:
        fn = (c or {}).get("function") or {}
        name = str(fn.get("name") or "").split(".")[-1]       # «tools.search», «functions.lines»
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if "reply" in args or "actions" in args:
            return json.dumps(args, ensure_ascii=False)
        if isinstance(args.get("tools"), list):
            tools += args["tools"]
        elif "name" in args and isinstance(args.get("args"), dict):
            tools.append({"name": args["name"], "args": args["args"]})
        elif name:
            tools.append({"name": name, "args": args})
    return json.dumps({"tools": tools}, ensure_ascii=False)


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    plain = {"reply": text.strip(), "actions": [], "tools": []}     # ответ не JSON — просто текст
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return plain
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return plain
    if not isinstance(d, dict):
        return plain
    d.setdefault("reply", "")
    if not isinstance(d.get("actions"), list):
        d["actions"] = []
    tools = d.get("tools")
    if isinstance(tools, dict):          # одна команда без списка
        tools = [tools]
    d["tools"] = [t for t in tools if isinstance(t, dict) and t.get("name")] if isinstance(tools, list) else []
    return d


# ---------------------------------------------------------------- контекст
_HEAD_WORDS = re.compile(r"глав|част|раздел|заголов|оглавл|содержан|chapter|пролог|эпилог|предислов|"
                        r"послеслов|вступлен|примечан|приложен|комментар|удали|убери|пропусти|не читай",
                        re.I)
_SENT_END = re.compile(r"[.,;:…!?»)\-–—]$")


def wants_headings(command: str) -> bool:
    return bool(_HEAD_WORDS.search(command or ""))


def heading_candidates(doc, limit: int = 400) -> list[int]:
    """Строки, похожие на заголовки: короткие абзацы без точки в конце или «Глава N»."""
    from .extract import is_chapter_heading
    out = []
    lines = doc.lines
    for i, ln in enumerate(lines):
        t = ln.text.strip()
        if not ln.para_start or not (2 <= len(t) <= 80) or not textproc.LETTER_RE.search(t):
            continue
        if textproc.TOC_RE.match(t):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        short_para = nxt is None or nxt.para_start
        if is_chapter_heading(t) or (short_para and not _SENT_END.search(t)):
            out.append(i)
            if len(out) >= limit:
                break
    return out


def build_context(doc, status, selected: list[int], cfg_filter: dict, replacements: list[dict],
                  want_headings: bool = False) -> str:
    """Что видит нейросеть: выделенные строки или выборка из книги, главы, текущие правила."""
    parts = []
    if doc is None:
        parts.append("Книга не открыта.")
    else:
        lines = doc.lines
        parts.append(f"Книга: {len(lines)} строк, {getattr(doc, 'page_count', 0) or '?'} стр.")
        if selected:
            idx = selected[:150]
            parts.append("Человек выделил эти строки (номер: текст):")
        else:
            n = len(lines)
            first = list(range(min(40, n)))
            step = max(1, n // 120)
            idx = sorted(set(first) | set(range(40, n, step)))
            parts.append("Выборка строк из книги (номер: текст; [не читается] — уже убрана фильтром):")
        budget = MAX_CONTEXT_CHARS
        for i in idx:
            t = lines[i].text[:300]
            mark = "" if (not status or status[i].included) else " [не читается]"
            s = f"{i}: {t}{mark}"
            budget -= len(s) + 1
            if budget < 0:
                parts.append("…")
                break
            parts.append(s)
    if doc is not None:
        chs = sorted(getattr(doc, "chapters", []) or [], key=lambda c: c.line)
        if chs:
            parts.append("Главы, которые нашла программа (номер строки: название):")
            parts += [f"{c.line}: {c.title[:100]}" for c in chs[:80]]
            if len(chs) > 80:
                parts.append(f"… всего глав {len(chs)}")
        else:
            parts.append("Главы программа не нашла.")
        if want_headings:
            heads = heading_candidates(doc)
            if heads:
                parts.append("Похожие на заголовки строки по всей книге (номер: текст):")
                budget = MAX_HEADINGS_CHARS
                for i in heads:
                    s = f"{i}: {doc.lines[i].text[:100]}"
                    budget -= len(s) + 1
                    if budget < 0:
                        parts.append("…")
                        break
                    parts.append(s)
        parts.append(f"Последняя строка книги — номер {len(doc.lines) - 1}.")
    rules = [f"- {r.get('kind')} «{r.get('pattern')}» ({r.get('action')})"
             for r in cfg_filter.get("rules", [])][:30]
    if rules:
        parts.append("Уже есть правила:\n" + "\n".join(rules))
    on = [k for k in OPTIONS if cfg_filter.get(k, textproc.DEFAULT_FILTER.get(k))]
    parts.append("Включены готовые фильтры: " + (", ".join(on) or "нет"))
    reps = [f"- {r.get('find')} → {r.get('replace')}" for r in replacements][:40]
    if reps:
        parts.append("Словарь произношения:\n" + "\n".join(reps))
    return "\n".join(parts)


# ---------------------------------------------------------------- проверка предложений
def _short(s: str, n: int = 90) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def check_rule(a: dict, doc, cfg_filter: dict) -> Proposal:
    kind = a.get("kind") if a.get("kind") in textproc.RULE_KINDS else "contains"
    action = a.get("action") if a.get("action") in textproc.RULE_ACTION else "line"
    where = a.get("where") if a.get("where") in textproc.RULE_WHERE else "any"
    pattern = str(a.get("pattern") or "")
    if action == "cut" and kind not in ("contains", "regex"):
        kind = "contains"
    rule = textproc.new_rule(pattern, kind, where, action, bool(a.get("case")))
    what = "Не читать строки" if action == "line" else "Вырезать из текста"
    title = f"{what}: {textproc.RULE_KINDS[kind].lower()} «{_short(pattern, 60)}»" \
            + (" (только верх/низ страницы)" if where == "edges" else "")
    p = Proposal({"type": "rule", "rule": rule}, title)
    if not pattern.strip():
        p.ok, p.detail = False, "пустое правило — пропущено"
        return p
    if kind == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            p.ok, p.detail = False, f"ошибка в выражении ({e}) — пропущено"
            return p
    if doc is None:
        p.detail = "книга не открыта — проверить не на чем"
        return p
    cr = textproc._CompiledRule(rule)
    edge = int(cfg_filter.get("edge_lines", 3))
    hits = [ln for ln in doc.lines if cr.applies_to(ln, edge) and cr.matches(ln.text)]
    total = len(doc.lines)
    if action == "line":
        p.detail = f"заденет строк: {len(hits)} из {total}"
    else:
        p.detail = f"найдено в строках: {len(hits)}"
    seen = []
    for ln in hits:
        s = _short(ln.text)
        if s not in seen:
            seen.append(s)
        if len(seen) == 3:
            break
    p.samples = seen
    if not hits:
        p.checked = False
        p.detail += " — в книге ничего не нашлось"
    elif action == "line" and len(hits) > total * 0.3:
        p.checked = False
        p.detail += " — это очень много, проверьте"
    return p


_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯaeiouyAEIOUY")


def stress_problem(rep: str) -> str:
    """Ошибка в разметке ударения: слабые модели ставят «+» перед каждым слогом
    («Ит+а+ли+ю») — синтезатор такое читает хуже, чем вовсе без словаря."""
    for w in re.findall(r"[^\s\-–—]+", rep):
        if w.count("+") > 1:
            return f"в слове «{w}» несколько знаков ударения, а нужен один"
    for m in re.finditer(r"\+(.?)", rep):
        if m.group(1) not in _VOWELS:
            return "знак + должен стоять перед ударной гласной"
    return ""


def check_replace(a: dict, text: str) -> Proposal:
    find, rep = str(a.get("find") or ""), str(a.get("replace") or "")
    regex = bool(a.get("regex"))
    entry = {"find": find, "replace": rep, "regex": regex, "case": bool(a.get("case")), "enabled": True}
    p = Proposal({"type": "replace", "entry": entry}, f"Произношение: «{_short(find, 40)}» → «{_short(rep, 40)}»")
    if not find.strip():
        p.ok, p.detail = False, "пустая запись — пропущено"
        return p
    bad = stress_problem(rep)
    if bad:
        p.ok, p.detail = False, bad + " — пропущено"
        return p
    try:
        if regex:
            rx = re.compile(find, 0 if entry["case"] else re.I)
        else:
            pat = re.escape(find)
            if re.match(r"\w", find) and re.search(r"\w$", find):
                pat = r"\b" + pat + r"\b"
            rx = re.compile(pat, 0 if entry["case"] else re.I)
    except re.error as e:
        p.ok, p.detail = False, f"ошибка в выражении ({e}) — пропущено"
        return p
    if text:
        n = len(rx.findall(text))
        p.detail = f"встречается в тексте: {n} раз"
        if not n:
            p.detail += " (на будущее)"
    return p


def check_option(a: dict, cfg_filter: dict) -> Proposal:
    key, val = a.get("key"), bool(a.get("value", True))
    if key not in OPTIONS:
        return Proposal({"type": "option"}, f"Неизвестный фильтр «{key}»", "пропущено", ok=False)
    cur = bool(cfg_filter.get(key, textproc.DEFAULT_FILTER.get(key)))
    p = Proposal({"type": "option", "key": key, "value": val},
                 f"{'Убирать' if val else 'Не убирать'}: {OPTIONS[key]}")
    if cur == val:
        p.detail, p.checked = "уже так", False
    return p


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def check_chapters(a: dict, doc) -> Proposal:
    pattern = str(a.get("pattern") or "").strip()
    p = Proposal({"type": "chapters"}, "Разметить главы заново")
    if doc is None:
        p.ok, p.detail = False, "книга не открыта — пропущено"
        return p
    lines = doc.lines
    if pattern:
        try:
            rx = re.compile(pattern, 0 if a.get("case") else re.I)
        except re.error as e:
            p.ok, p.detail = False, f"ошибка в выражении ({e}) — пропущено"
            return p
        p.title += f" по образцу «{_short(pattern, 50)}»"
        idx = [i for i, ln in enumerate(lines)
               if ln.para_start and len(ln.text.strip()) <= 120 and rx.search(ln.text.strip())]
    else:
        raw = a.get("lines") if isinstance(a.get("lines"), list) else []
        idx = sorted({i for i in (_int(x) for x in raw) if i is not None and 0 <= i < len(lines)})
    chapters: list[tuple[int, str]] = []
    for i in idx:
        title = lines[i].text.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        # «Глава 1» / «Начало» — название продолжается следующей строкой
        if nxt and nxt.page == lines[i].page and not nxt.para_start and len(title) + len(nxt.text) < 90:
            title += ". " + nxt.text.strip()
        if chapters and chapters[-1][1].lower() == title.lower():
            continue        # повторяющийся колонтитул — только первое появление
        chapters.append((i, title[:120]))
    p.data["chapters"] = chapters
    old_n = len(getattr(doc, "chapters", []) or [])
    p.detail = f"глав: {len(chapters)} (сейчас найдено программой: {old_n})"
    if len(chapters) > 4:
        p.samples = [_short(t, 70) for _, t in chapters[:3]] + ["…", _short(chapters[-1][1], 70)]
    else:
        p.samples = [_short(t, 70) for _, t in chapters]
    if not chapters:
        p.ok, p.detail = False, "в книге ничего не нашлось — пропущено"
    elif len(chapters) < 2:
        p.checked = False
        p.detail += " — меньше двух, проверьте"
    elif len(chapters) > 300:
        p.checked = False
        p.detail += " — очень много, похоже, это не главы"
    return p


def check_delete(a: dict, doc, status) -> Proposal:
    p = Proposal({"type": "delete"}, "Не читать кусок книги")
    if doc is None:
        p.ok, p.detail = False, "книга не открыта — пропущено"
        return p
    n = len(doc.lines)
    a_from, a_to = _int(a.get("from")), _int(a.get("to"))
    if a_to is not None and a_to < 0:
        a_to = n - 1
    if a_from is None or a_to is None or not (0 <= a_from < n) or a_to < a_from:
        p.ok, p.detail = False, f"непонятные границы ({a.get('from')}–{a.get('to')}) — пропущено"
        return p
    a_to = min(a_to, n - 1)
    p.data.update({"from": a_from, "to": a_to})
    count = a_to - a_from + 1
    read = sum(1 for i in range(a_from, a_to + 1) if not status or status[i].included)
    pg = (doc.lines[a_from].page + 1, doc.lines[a_to].page + 1)
    label = {"Страница": "стр.", "Глава": "гл.", "Слайд": "слайд", "Лист": "лист"}.get(
        getattr(doc, "page_label", "") or "Страница", "стр.")
    where = f"{label} {pg[0]}" + (f"–{pg[1]}" if pg[1] != pg[0] else "")
    p.title = f"Не читать строки {a_from}–{a_to} ({where})"
    p.detail = f"строк: {count}, из них сейчас читаются: {read}"
    p.samples = ["начало: " + _short(doc.lines[a_from].text, 80)]
    if a_to != a_from:
        p.samples.append("конец: " + _short(doc.lines[a_to].text, 80))
    if a_to + 1 < n:
        p.samples.append("дальше читается: " + _short(doc.lines[a_to + 1].text, 70))
    if not read:
        p.checked = False
        p.detail += " — и так не читаются"
    elif count > n * 0.5:
        p.checked = False
        p.detail += " — это больше половины книги, проверьте"
    return p


def check_edit(a: dict, doc) -> Proposal:
    """Правка одной строки: «find» → «replace» (или вся строка целиком — «text»)."""
    p = Proposal({"type": "edit"}, "Исправить строку")
    if doc is None:
        p.ok, p.detail = False, "книга не открыта — пропущено"
        return p
    i = _int(a.get("line"))
    if i is None or not (0 <= i < len(doc.lines)):
        p.ok, p.detail = False, f"нет строки с номером {a.get('line')} — пропущено"
        return p
    old = doc.lines[i].text
    find, rep = a.get("find"), a.get("replace")
    if isinstance(find, str) and find:
        if find not in old:
            p.ok, p.detail = False, f"в строке {i} нет «{_short(find, 40)}» — пропущено"
            return p
        new = old.replace(find, str(rep or ""), 1)
        p.data.update({"line": i, "find": find, "replace": str(rep or "")})
    elif isinstance(a.get("text"), str):
        new = a["text"]
        p.data.update({"line": i, "text": new})
    else:
        p.ok, p.detail = False, "непонятно, что заменить — пропущено"
        return p
    p.title = f"Исправить строку {i}"
    p.samples = ["было:  " + _short(old, 110), "стало: " + _short(new, 110)]
    if new == old:
        p.ok, p.detail = False, "ничего не меняется — пропущено"
    elif len(new) < len(old) * 0.5 and len(old) > 20:
        p.checked = False
        p.detail = "строка сильно укорачивается — проверьте"
    p.detail = (p.detail + "; " if p.detail else "") + "действует, пока книга открыта"
    return p


def check_rename(a: dict, doc, pending_lines: set[int]) -> Proposal:
    i, title = _int(a.get("line")), " ".join(str(a.get("title") or "").split())[:120]
    p = Proposal({"type": "rename_chapter"}, f"Переименовать главу: «{_short(title, 60)}»")
    if doc is None:
        p.ok, p.detail = False, "книга не открыта — пропущено"
        return p
    cur = {c.line: c.title for c in getattr(doc, "chapters", []) or []}
    if not title or i is None or (i not in cur and i not in pending_lines):
        p.ok, p.detail = False, f"в строке {a.get('line')} глава не начинается — пропущено"
        return p
    p.data.update({"line": i, "title": title})
    if i in cur:
        p.samples = [f"было: {_short(cur[i], 80)}"]
    return p


# ---------------------------------------------------------------- настройки программы
# ключ для нейросети → (раздел настроек, поле, тип, допустимое, подпись)
# раздел "" — верхний уровень; допустимое: (мин, макс) для чисел, ключ env["choices"] для списков
SETTINGS = {
    "engine": ("", "engine", "choice", "engines", "Движок речи"),
    "voice": ("voices", None, "voice", None, "Голос"),
    "speed": ("params", "speed", float, (0.5, 2.5), "Скорость речи"),
    "pitch": ("params", "pitch", float, (-1.0, 1.0), "Высота голоса"),
    "volume": ("params", "volume", float, (0.1, 2.0), "Громкость"),
    "sentence_pause_ms": ("params", "sentence_pause_ms", int, (0, 3000), "Пауза между предложениями, мс"),
    "paragraph_pause_ms": ("params", "paragraph_pause_ms", int, (0, 5000), "Пауза между абзацами, мс"),
    "export_format": ("export", "format", "choice", "export_format", "Формат файла"),
    "export_quality": ("export", "quality", "choice", "export_quality", "Качество звука"),
    "split_mode": ("export", "split_mode", "choice", "split_mode", "Деление на файлы"),
    "split_minutes": ("export", "split_minutes", int, (1, 600), "Длина одного файла, мин"),
    "artist": ("export", "artist", str, None, "Исполнитель в тегах файла"),
    "numbers": ("text", "numbers", "choice", "auto_on_off", "Числа словами"),
    "translit": ("text", "translit", "choice", "auto_on_off", "Латиницу — русскими буквами"),
    "abbreviations": ("text", "abbreviations", bool, None, "Раскрывать сокращения"),
    "symbols": ("text", "symbols", bool, None, "Читать символы (№, %, °)"),
    "smart_accent": ("text", "smart_accent", bool, None, "Умные ударения"),
    "join_lines": ("text", "join_lines", bool, None, "Склеивать строки в абзацы"),
    "fix_hyphen": ("text", "fix_hyphen", bool, None, "Убирать переносы слов"),
    "merge_broken": ("text", "merge_broken", bool, None, "Склеивать абзацы, разорванные страницей"),
}


def _env_value(env: dict, key: str):
    sec, fld = SETTINGS[key][0], SETTINGS[key][1]
    if key == "voice":
        return env.get("voices", {}).get(env.get("engine"))
    return env.get(fld) if sec == "" else env.get(sec, {}).get(fld)


def settings_text(env: dict | None) -> str:
    """Раздел «Настройки» для нейросети: ключ, текущее значение, что можно."""
    if not env:
        return ""
    out = ["Настройки (ключ: сейчас; можно):"]
    engines = env.get("engines", {})
    for key, (sec, fld, typ, allowed, label) in SETTINGS.items():
        if key == "voice":
            for eid, e in engines.items():
                if not e.get("voices"):
                    continue
                vs = ", ".join(f"{vid} ({name})" for vid, name in e["voices"][:25])
                out.append(f"- voice для engine={eid}: сейчас {env.get('voices', {}).get(eid) or '—'}; {vs}")
            continue
        cur = _env_value(env, key)
        if typ == "choice":
            if allowed == "engines":
                can = " | ".join(f"{eid} ({e['title']}{'' if e.get('available') else ', не установлен'})"
                                 for eid, e in engines.items())
            else:
                can = " | ".join(map(str, env.get("choices", {}).get(allowed, [])))
        elif isinstance(allowed, tuple):
            can = f"{allowed[0]}…{allowed[1]}"
        elif typ is bool:
            can = "true/false"
        else:
            can = "текст"
        out.append(f"- {key} — {label}: {cur!r}; {can}")
    return "\n".join(out)


def check_setting(a: dict, env: dict | None) -> Proposal:
    key = a.get("key")
    if not env or key not in SETTINGS:
        return Proposal({"type": "setting"}, f"Неизвестная настройка «{key}»", "пропущено", ok=False)
    sec, fld, typ, allowed, label = SETTINGS[key]
    val = a.get("value")
    p = Proposal({"type": "setting", "key": key}, label)
    try:
        if key == "voice":
            eid = a.get("engine") or env.get("engine")
            e = env.get("engines", {}).get(eid)
            names = dict(e.get("voices", [])) if e else {}
            if str(val) not in names:
                raise ValueError(f"голоса «{val}» нет у движка «{eid}»")
            val = str(val)
            p.data.update({"sec": "voices", "field": eid, "value": val})
            p.title = f"Голос: {names[val]}"
            cur = env.get("voices", {}).get(eid)
            if cur is None:
                cur = ""
            if eid != env.get("engine"):
                p.detail = f"для движка «{e['title']}» (сейчас выбран другой)"
        else:
            if typ == "choice":
                opts = list(env.get("engines", {})) if allowed == "engines" \
                    else list(env.get("choices", {}).get(allowed, []))
                if val not in opts:
                    raise ValueError(f"допустимо: {', '.join(map(str, opts))}")
                if allowed == "engines" and not env["engines"][val].get("available"):
                    p.checked = False
                    p.detail = "этот движок на компьютере не установлен"
            elif typ is bool:
                if isinstance(val, str):
                    val = val.strip().lower() in ("true", "1", "да", "on", "вкл")
                val = bool(val)
            elif typ in (int, float):
                v = float(str(val).replace(",", "."))
                lo, hi = allowed
                if not (lo <= v <= hi):
                    raise ValueError(f"нужно от {lo} до {hi}")
                val = int(round(v)) if typ is int else round(v, 3)
            else:
                val = str(val or "")[:200]
            p.data.update({"sec": sec, "field": fld, "value": val})
            cur = _env_value(env, key)
            p.title = f"{label}: {_human(env, key, val)}"
    except (TypeError, ValueError) as e:
        p.ok, p.detail = False, f"{e} — пропущено"
        return p
    if cur == val:
        p.checked = False
        p.detail = (p.detail + "; " if p.detail else "") + "уже так"
    else:
        p.samples = [f"сейчас: {_human(env, key, cur)}"]
    return p


def _human(env: dict, key: str, val) -> str:
    """Значение настройки словами: «По главам…» вместо chapters, «вкл.» вместо True."""
    if val is None or val == "":
        return "—"
    if isinstance(val, bool):
        return "вкл." if val else "выкл."
    if key == "engine":
        return env.get("engines", {}).get(val, {}).get("title", str(val))
    if key == "voice":
        for e in env.get("engines", {}).values():
            names = dict(e.get("voices", []))
            if val in names:
                return names[val]
    return str(env.get("labels", {}).get(key, {}).get(val, val))


# ---------------------------------------------------------------- инструменты
MAX_STEPS = 6              # шагов с инструментами на одну просьбу
MAX_TOOL_CALLS = 5         # вызовов за один шаг
MAX_TOOL_CHARS = 4000      # длина результата одного вызова

_LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z'\-]{1,}\b")
_ABBR_WORD = re.compile(r"\b[A-ZА-ЯЁ]{2,6}\b")
_CAP_WORD = re.compile(r"(?<![.!?…«\"]\s)(?<!^)\b[А-ЯЁ][а-яё]{2,}\b")


class BookTools:
    """Что нейросеть может посмотреть в книге. Только чтение — ничего не меняет."""

    def __init__(self, doc, status, cfg_filter: dict):
        self.doc, self.status, self.cfg_filter = doc, status, cfg_filter or {}

    def _row(self, i: int, n: int = 200) -> str:
        t = self.doc.lines[i].text
        t = t if len(t) <= n else t[:n - 1] + "…"
        mark = "" if (not self.status or self.status[i].included) else " [не читается]"
        return f"{i}: {t}{mark}"

    def _fit(self, rows: list[str], head: str) -> str:
        out, budget = [head], MAX_TOOL_CHARS
        for r in rows:
            budget -= len(r) + 1
            if budget < 0:
                out.append("… (дальше не поместилось — сузь запрос)")
                break
            out.append(r)
        return "\n".join(out)

    def call(self, name: str, args: dict) -> tuple[str, str]:
        """(результат для нейросети, подпись для человека)."""
        if self.doc is None:
            return "Книга не открыта.", name
        args = args if isinstance(args, dict) else {}
        fn = {"search": self.search, "lines": self.lines, "test_rule": self.test_rule,
              "headings": self.headings, "words": self.words}.get(name)
        if not fn:
            return f"Нет инструмента «{name}». Есть: search, lines, test_rule, headings, words.", name
        try:
            return fn(**{k: v for k, v in args.items() if isinstance(k, str)})
        except TypeError as e:
            return f"Неверные параметры для {name}: {e}", name

    def search(self, pattern="", regex=False, limit=30, **_):
        pattern = str(pattern or "")
        if not pattern.strip():
            return "Пустой образец.", "поиск"
        limit = max(1, min(_int(limit, 30) or 30, 80))
        if regex:
            try:
                rx = re.compile(pattern, re.I)
            except re.error as e:
                return f"Ошибка в выражении: {e}", f"поиск /{_short(pattern, 30)}/"
            hit = lambda t: rx.search(t)      # noqa: E731
        else:
            key = pattern.lower().replace("ё", "е")
            hit = lambda t: key in t.lower().replace("ё", "е")   # noqa: E731
        found = [i for i, ln in enumerate(self.doc.lines) if hit(ln.text)]
        head = f"Найдено строк: {len(found)}" + (f", показаны первые {limit}:" if len(found) > limit else ":")
        return self._fit([self._row(i) for i in found[:limit]], head), f"поиск «{_short(pattern, 30)}»: {len(found)}"

    def lines(self, **kw):
        n = len(self.doc.lines)
        a, b = _int(kw.get("from"), 0), _int(kw.get("to"), None)
        if b is None or b < 0:
            b = min(n - 1, a + 40) if b is None else n - 1
        a, b = max(0, a), min(n - 1, b)
        if b < a:
            return f"Пустой диапазон (в книге строки 0–{n - 1}).", "строки"
        b = min(b, a + 59)
        return self._fit([self._row(i, 300) for i in range(a, b + 1)], f"Строки {a}–{b}:"), f"строки {a}–{b}"

    def test_rule(self, **kw):
        p = check_rule(kw, self.doc, self.cfg_filter)
        if not p.ok:
            return f"Правило не годится: {p.detail}", "проверка правила"
        rule = p.data["rule"]
        cr = textproc._CompiledRule(rule)
        edge = int(self.cfg_filter.get("edge_lines", 3))
        hits = [i for i, ln in enumerate(self.doc.lines) if cr.applies_to(ln, edge) and cr.matches(ln.text)]
        reading = sum(1 for i in hits if not self.status or self.status[i].included)
        head = (f"{p.title}. Заденет строк: {len(hits)} из {len(self.doc.lines)} "
                f"(сейчас из них читаются: {reading}). Примеры:")
        return self._fit([self._row(i, 150) for i in hits[:12]], head), f"проверка правила: {len(hits)} строк"

    def headings(self, **_):
        idx = heading_candidates(self.doc)
        return self._fit([self._row(i, 100) for i in idx], f"Похожих на заголовки строк: {len(idx)}"), \
            f"заголовки: {len(idx)}"

    def words(self, kind="latin", limit=60, **_):
        from collections import Counter
        limit = max(5, min(_int(limit, 60) or 60, 150))
        rx = {"latin": _LATIN_WORD, "abbr": _ABBR_WORD, "names": _CAP_WORD}.get(kind, _LATIN_WORD)
        cnt: Counter = Counter()
        for i, ln in enumerate(self.doc.lines):
            if self.status and not self.status[i].included:
                continue
            cnt.update(m.group(0) for m in rx.finditer(ln.text))
        if kind == "names":     # слово с большой буквы, которое встречается и с маленькой, — не имя
            low = Counter()
            for ln in self.doc.lines:
                low.update(w for w in re.findall(r"\b[а-яё]{3,}\b", ln.text))
            cnt = Counter({w: c for w, c in cnt.items() if low[w.lower()] < c // 3 + 1})
        top = cnt.most_common(limit)
        label = {"latin": "латиницей", "abbr": "аббревиатур", "names": "имён и названий"}.get(kind, kind)
        return (f"Самые частые слова ({label}), всего разных: {len(cnt)}:\n"
                + ", ".join(f"{w} ×{c}" for w, c in top)), f"слова {label}: {len(cnt)}"


# ---------------------------------------------------------------- самопроверка
MAX_REVIEWS = 2            # сколько раз просить нейросеть исправить ошибки в ответе


def review(props: list[Proposal]) -> list[str]:
    """Замечания к предложениям, которые нейросеть может исправить сама: правило ничего не
    находит или задевает слишком много, нет такой строки, такой настройки и т. п."""
    out = []
    for p in props:
        typ = p.data.get("type")
        detail = p.detail.split("\nЗачем:")[0]
        if not p.ok:
            out.append(f"«{p.title}»: {detail}")
        elif typ == "rule" and ("ничего не нашлось" in detail or "очень много" in detail):
            out.append(f"«{p.title}»: {detail}. Проверь его инструментом test_rule"
                       + (" (может, нужен \"kind\": \"regex\"?)" if "ничего" in detail else ""))
        elif typ == "chapters" and ("меньше двух" in detail or "очень много" in detail):
            out.append(f"«{p.title}»: {detail}")
        elif typ == "delete" and "больше половины" in detail:
            out.append(f"«{p.title}»: {detail} — точно ли такие границы?")
    return out


# ---------------------------------------------------------------- разговор
def _memory(d: dict) -> str:
    """Короткий окончательный ответ — для истории разговора (не раздуваем запросы)."""
    acts = [{k: v for k, v in a.items() if k != "why"} for a in d.get("actions", [])[:12] if isinstance(a, dict)]
    s = json.dumps({"reply": d.get("reply", ""), "actions": acts}, ensure_ascii=False)
    return s if len(s) <= 3000 else s[:2990] + "…"


def run(cfg: dict, command: str, context: str, doc, cfg_filter: dict, text: str,
        status=None, history: list[dict] | None = None, env: dict | None = None,
        progress=None, stop_flag=None) -> Answer:
    """Просьба → (несколько шагов с инструментами) → предложения.
    history — прошлые реплики разговора [{"role", "content"}]; env — настройки программы и голоса."""
    t = time.time()
    system = SYSTEM.replace("<<OPTIONS>>", ", ".join(OPTIONS))
    st = settings_text(env)
    user = f"Просьба: {command}\n\n--- Книга и программа сейчас ---\n{context}" + (f"\n\n{st}" if st else "")
    msgs = [{"role": "system", "content": system}] + list(history or []) + [{"role": "user", "content": user}]
    tools = BookTools(doc, status, cfg_filter)
    looked: list[str] = []
    log.info(f"Нейросеть: запрос ({len(context)} симв. контекста, истории {len(history or [])} реплик)")
    requests, reviews, props = 0, 0, []
    d: dict = {"reply": "", "actions": [], "tools": []}
    model = via = ""
    for step in range(MAX_STEPS + 1):
        if stop_flag and stop_flag():
            raise InterruptedError("Отменено")
        if progress:
            progress("Исправляю по замечаниям программы…" if reviews and not d["tools"] else
                     f"Думаю… (шаг {step + 1})" if not looked else f"Думаю… смотрел: {looked[-1]}")
        raw, model, via = chat_any(cfg, msgs)
        requests += 1
        d = _parse_json(raw)
        if not d["tools"] or d["actions"]:
            props = proposals_from(d["actions"], doc, cfg_filter, text, status, env)
            problems = review(props)
            if not problems or reviews >= MAX_REVIEWS or step == MAX_STEPS:
                break
            # программа нашла ошибки в предложениях — пусть нейросеть исправит их сама, до показа человеку
            reviews += 1
            looked.append(f"исправление ошибок ({len(problems)})")
            log.info(f"Нейросеть: замечания к ответу ({len(problems)}), прошу исправить")
            msgs += [{"role": "assistant", "content": raw[:6000]},
                     {"role": "user", "content": "Программа проверила твой ответ, есть замечания:\n"
                      + "\n".join("- " + x for x in problems)
                      + "\n\nИсправь эти действия (можно сначала посмотреть книгу инструментами) или убери их, "
                        "если они не нужны. Верни окончательный ответ целиком — со всеми действиями, "
                        "и исправленными, и теми, что были в порядке."}]
            continue
        if step == MAX_STEPS:
            break
        results = []
        for call in d["tools"][:MAX_TOOL_CALLS]:
            res, label = tools.call(str(call.get("name")), call.get("args") or {})
            looked.append(label)
            results.append(f"### {call.get('name')} {json.dumps(call.get('args') or {}, ensure_ascii=False)[:200]}\n{res}")
        log.info(f"Нейросеть: шаг {step + 1}, инструменты: {'; '.join(looked[-len(results):])}")
        last = step + 1 == MAX_STEPS
        msgs += [{"role": "assistant", "content": raw[:4000]},
                 {"role": "user", "content": "Результаты:\n\n" + "\n\n".join(results)
                  + ("\n\nИнструменты больше недоступны — дай окончательный ответ с actions."
                     if last else "\n\nПродолжай: ещё инструменты или окончательный ответ.")}]
    if d["tools"] and not d["actions"] and not d.get("reply"):
        d["reply"] = "Помощник не успел закончить поиск. Попробуйте сформулировать просьбу точнее."
    sec = time.time() - t
    log.info(f"Нейросеть: ответ за {sec:.0f} с, запросов {requests}, предложений: {len(props)}")
    return Answer(str(d.get("reply") or "").strip(), props, sec, model, via,
                  memory=_memory(d), looked=looked, requests=requests)


def proposals_from(actions: list, doc, cfg_filter: dict, text: str, status=None,
                   env: dict | None = None, limit: int = 60) -> list[Proposal]:
    props = []
    pending_chapters: set[int] = set()
    for a in actions[:limit]:
        if not isinstance(a, dict):
            continue
        typ = a.get("type")
        if typ == "rule":
            p = check_rule(a, doc, cfg_filter)
        elif typ == "replace":
            p = check_replace(a, text)
        elif typ == "option":
            p = check_option(a, cfg_filter)
        elif typ == "chapters":
            p = check_chapters(a, doc)
            pending_chapters |= {i for i, _ in p.data.get("chapters", [])}
        elif typ == "delete":
            p = check_delete(a, doc, status)
        elif typ == "edit":
            p = check_edit(a, doc)
        elif typ == "rename_chapter":
            p = check_rename(a, doc, pending_chapters)
        elif typ == "setting":
            p = check_setting(a, env)
        else:
            continue
        why = str(a.get("why") or "").strip()
        if why:
            p.detail = (p.detail + "\n" if p.detail else "") + "Зачем: " + why
        props.append(p)
    return props


# ---------------------------------------------------------------- проход по всей книге
SCAN_SYSTEM = """Ты помогаешь синтезатору русской речи правильно читать книгу.
В отрывке найди слова, которые синтезатор, скорее всего, прочитает НЕПРАВИЛЬНО:
иностранные имена, фамилии, названия и слова (в том числе латиницей), редкие и устаревшие слова,
термины, аббревиатуры, которые читаются не по буквам или наоборот по буквам.
Обычные русские слова НЕ включай — их ударения программа ставит сама по словарю.
Для каждого слова: "find" — как оно написано в тексте (ровно эта форма, одно слово), "replace" — как
читать: русскими буквами, с ОДНИМ знаком + перед ударной гласной в каждом слове. Слоги НЕ разделяй.
Правильно: Гёте → Г+ёте, Шекспира → Шексп+ира, Палладио → Палл+адио, Weimar → В+еймар,
NASA → Н+аса, ЖЗЛ → жэзэ+эл, РГБ → эргэб+э.
Неправильно: Ит+а+ли+ю, Ж+З+Л, Пал+ла+дио (несколько знаков +).
Если слово в тексте склоняется, дай отдельные записи для встреченных форм.
Ответь ТОЛЬКО JSON: {"items": [{"find": "...", "replace": "...", "why": "кратко"}]}
Если таких слов нет — {"items": []}. Не больше 40 записей."""


def book_chunks(doc, included: list[bool] | None, chunk_chars: int = 12000,
                only: list[int] | None = None) -> list[str]:
    """Текст книги (только читаемые строки) кусками примерно по chunk_chars символов."""
    idx = only if only else range(len(doc.lines))
    chunks, cur, size = [], [], 0
    for i in idx:
        if included is not None and not included[i]:
            continue
        t = doc.lines[i].text.strip()
        if not t:
            continue
        if size + len(t) > chunk_chars and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def estimate_tokens(chunks: list[str]) -> int:
    """Грубо: русский текст — около 3 символов на токен, плюс инструкция и ответ на каждый кусок."""
    return sum(len(c) for c in chunks) // 3 + len(chunks) * 900


def scan_pronunciation(cfg: dict, chunks: list[str], replacements: list[dict], text: str,
                       progress=None, stop_flag=None) -> Answer:
    """Проходит книгу кусками и собирает записи словаря произношения.
    Прерванный проход (кнопка или лимит) возвращает то, что успело найтись."""
    t = time.time()
    have = {str(r.get("find", "")).lower() for r in replacements}
    found: dict[str, dict] = {}
    fails, done, model, via, stopped = [], 0, "", "", ""
    for n, chunk in enumerate(chunks):
        if stop_flag and stop_flag():
            stopped = "остановлено"
            break
        if progress:
            progress(f"Читаю книгу: часть {n + 1} из {len(chunks)}, найдено слов: {len(found)}")
        msgs = [{"role": "system", "content": SCAN_SYSTEM}, {"role": "user", "content": chunk}]
        try:
            # запас на размышления: «думающие» модели иначе тратят весь ответ на них и молчат
            raw, model, via = chat_any(cfg, msgs, max_tokens=8000)
        except AIError as e:
            fails.append(str(e))
            if "Слишком много запросов" in str(e) or "оплату" in str(e) or "Ключ не подошёл" in str(e):
                stopped = str(e).split("\n")[0]
                break
            continue
        done += 1
        d = _parse_json(raw)
        items = d.get("items") if isinstance(d.get("items"), list) else d.get("actions", [])
        for it in items:
            if not isinstance(it, dict):
                continue
            f, r = str(it.get("find") or "").strip(), str(it.get("replace") or "").strip()
            if not f or not r or f.lower() in have or f.lower() in found or f.lower() == r.lower():
                continue
            found[f.lower()] = {"type": "replace", "find": f, "replace": r, "why": it.get("why", "")}
    if not done and fails:
        raise AIError("Нейросеть не ответила ни на одну часть книги.\n" + fails[0])
    props = proposals_from(list(found.values()), None, {}, text, limit=400)
    dropped = sum(1 for p in props if not p.ok)
    props = [p for p in props if p.ok]       # ошибочную разметку не показываем вовсе

    def hits(p):
        m = re.search(r"(\d+) раз", p.detail)
        return int(m.group(1)) if m else 0
    props.sort(key=hits, reverse=True)
    for p in props:
        if not hits(p):
            p.checked = False       # слово не нашлось в тексте целиком — вероятно, ошибка
    reply = f"Прочитано частей: {done} из {len(chunks)}. Предлагаю записей произношения: {len(props)}."
    if dropped:
        reply += f" Отброшено записей с ошибочной разметкой ударения: {dropped}."
    if stopped:
        reply += f" Проход прерван ({stopped}) — показываю то, что успел найти."
    if fails and not stopped:
        reply += f" Не ответила нейросеть на частей: {len(fails)}."
    sec = time.time() - t
    log.info(f"Нейросеть: проход по книге за {sec:.0f} с, частей {done}/{len(chunks)}, записей {len(props)}")
    return Answer(reply, props, sec, model, via, memory=json.dumps({"reply": reply}, ensure_ascii=False),
                  requests=done + len(fails))


def list_models(cfg: dict) -> list[tuple[str, bool]]:
    """(id, бесплатная ли) — для выбора модели."""
    url = cfg.get("url", DEFAULT_URL).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.get('key', '').strip()}",
                                               "User-Agent": f"DocVoice/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AIError("Ключ не подошёл." if e.code == 401 else f"Сервер ответил ошибкой {e.code}.") from None
    except (urllib.error.URLError, OSError):
        raise AIError("Нет связи с сервером нейросети.") from None
    out = []
    for m in d.get("data", []):
        pr = m.get("pricing") or {}
        free = all(str(pr.get(k, "0")) in ("0", "0.0") for k in ("prompt", "completion"))
        out.append((m.get("id", ""), free))
    return sorted(out, key=lambda x: (not x[1], x[0]))
