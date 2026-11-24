"""Фильтрация строк, сборка абзацев и нормализация текста перед синтезом."""
from __future__ import annotations

import copy
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .extract import Document, Line

# --------------------------------------------------------------------------
# настройки фильтра по умолчанию
# --------------------------------------------------------------------------
DEFAULT_FILTER = {
    "rules": [],                    # пользовательские правила (см. Rule)
    "page_numbers": True,           # строки-номера страниц: "12", "- 12 -", "стр. 5", "5 из 120"
    "url_lines": True,              # строки, состоящие только из ссылки / email
    "url_inline": True,             # вырезать ссылки и email внутри текста
    "no_letters": True,             # строки без единой буквы
    "footnote_marks": True,         # сноски [12], ¹²³, *
    "footnote_numbers": True,       # приклеенные номера сносок по порядку: «…»1087, слово12
    "footnote_lines": True,         # абзацы-сноски «18 Кол. 2:3» с номерами по порядку
    "toc_lines": True,              # строки оглавления "Глава 1 ........ 12"
    "square_brackets": False,       # вырезать всё в [квадратных скобках]
    "parentheses": False,           # вырезать всё в (круглых скобках)
    "short_lines": 0,               # исключать строки короче N символов (0 — выкл.)
    "edge_lines": 3,                # сколько строк сверху/снизу страницы считать колонтитулом
}

RULE_KINDS = {
    "contains": "Содержит",
    "exact": "Совпадает целиком",
    "startswith": "Начинается с",
    "template": "Шаблон (цифры — любые)",
    "regex": "Регулярное выражение",
}
RULE_WHERE = {"any": "Везде", "edges": "Только верх/низ страницы"}
RULE_ACTION = {"line": "Убрать строку", "cut": "Вырезать совпадение"}


def new_rule(pattern: str = "", kind: str = "contains", where: str = "any",
             action: str = "line", case: bool = False, enabled: bool = True) -> dict:
    return {"pattern": pattern, "kind": kind, "where": where, "action": action,
            "case": case, "enabled": enabled}


# --------------------------------------------------------------------------
# нормализация для сравнения колонтитулов
# --------------------------------------------------------------------------
_DIGITS = re.compile(r"\d+")
_ROMAN = re.compile(r"\b[IVXLCDM]{1,7}\b")


def template_key(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = _DIGITS.sub("#", s)
    s = re.sub(r"[\s ]+", " ", s)
    s = re.sub(r"^[\s\-–—_.·•|]+|[\s\-–—_.·•|]+$", "", s)
    return s.strip()


# --------------------------------------------------------------------------
# регулярки пресетов
# --------------------------------------------------------------------------
URL_RE = re.compile(
    r"(?:(?:https?|ftp)://|www\.)[^\s<>«»\"']+"
    r"|\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"
    r"|\b(?:[a-z0-9-]+\.)+(?:ru|com|org|net|рф|su|io|info|edu|gov|ua|by|kz|de|uk)(?:/[^\s<>«»\"']*)?",
    re.I)
PAGE_NUM_RE = re.compile(
    r"^[\s\-–—\[\(|]*(?:(?:стр(?:аница)?|с|page|p|pg)\.?\s*)?"
    r"(?:\d{1,5}|[ivxlcdm]{1,7})"
    r"(?:\s*(?:/|из|of)\s*\d{1,5})?[\s\-–—\]\)|]*$",
    re.I)
TOC_RE = re.compile(r"^.{2,}?(?:\s*[.·…_]{4,}\s*|\s{3,})\d{1,4}\s*$")
FOOTNOTE_RE = re.compile(r"\[\d{1,3}(?:[,\-–]\s*\d{1,3})*\]|[¹²³⁰-⁹]+|(?<=\w)\*+(?=\W|$)|↩︎?")
LETTER_RE = re.compile(r"[^\W\d_]")
# число вплотную после буквы или закрывающего знака («…»1087, слово12, конец.13);
# не трогаем дроби и время (3,14 / 12:30), «Ту-154», «№5» и числа в начале строки
FOOTNOTE_NUM_RE = re.compile(
    r"(?:(?<=[^\W\d_])|(?<=[»\"”’)\].!?…;:,]))(?<!\d[.,:])(\d{1,4})(?=[\s.,;:!?…»\"”’)\]]|$)")


def find_footnote_numbers(doc: Document) -> dict[int, list[tuple[int, int]]]:
    """Приклеенные номера сносок: {индекс строки: [(начало, конец), …]}.

    Число засчитывается сноской, только если соседнее такое же число в тексте
    (предыдущее или следующее) больше или меньше его на 1–3: сноски идут по
    порядку, а случайные «слово2024» или «Ту154» — нет."""
    found = []    # (строка, начало, конец, число) в порядке текста
    for li, ln in enumerate(doc.lines):
        for m in FOOTNOTE_NUM_RE.finditer(ln.text):
            found.append((li, m.start(1), m.end(1), int(m.group(1))))
    res: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for k, (li, a, b, n) in enumerate(found):
        prev_ok = k > 0 and 1 <= n - found[k - 1][3] <= 3
        next_ok = k + 1 < len(found) and 1 <= found[k + 1][3] - n <= 3
        if prev_ok or next_ok:
            res[li].append((a, b))
    return res


_FN_LINE_RE = re.compile(r"^\s*(\d{1,3})\s+\S")


def find_footnote_lines(doc: Document) -> set[int]:
    """Абзацы-сноски, попавшие в текст (DOCX/PDF, сделанные из вёрстки): «18 Кол. 2:3»,
    «17 Так же, как и в других алфавитах…». Строка засчитывается сноской, только если
    номера соседних таких строк отличаются на 1–3 — сноски идут по порядку. Сноска,
    оборванная без точки, продолжается следующим абзацем, если тот не со строчной буквы."""
    lines = doc.lines
    found = []     # (строка, номер)
    for li, ln in enumerate(lines):
        if not ln.para_start or len(ln.text) > 400:
            continue
        m = _FN_LINE_RE.match(ln.text)
        if m:
            found.append((li, int(m.group(1))))
    res: set[int] = set()
    for k, (li, n) in enumerate(found):
        prev_ok = k > 0 and 1 <= n - found[k - 1][1] <= 3
        next_ok = k + 1 < len(found) and 1 <= found[k + 1][1] - n <= 3
        if prev_ok or next_ok:
            res.add(li)
    for li in sorted(res):
        cur = li
        for k in range(4):
            t = lines[cur].text.rstrip()
            nxt = cur + 1
            if (nxt >= len(lines) or nxt in res or _SENT_END.search(t) or t[-1:].isdigit()
                    or lines[nxt].page != lines[cur].page or not lines[nxt].para_start):
                break
            nt = lines[nxt].text.strip()
            if not nt or nt[:1].islower() or len(nt) > 1000:
                break       # со строчной — это продолжение основного текста, а не сноски
            if k == 0 and _SENT_END.search(nt):
                # сноска без точки в конце, а следующий абзац — законченный: это может быть
                # и хвост сноски, и обычный текст. Лучше прочитать лишнее, чем потерять текст.
                break
            res.add(nxt)
            cur = nxt
    return res


def cut_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for a, b in sorted(spans, reverse=True):
        text = text[:a] + text[b:]
    return text


@dataclass
class LineStatus:
    included: bool
    reason: str = ""
    auto: bool = True   # True — решено фильтром, False — ручная правка


class _CompiledRule:
    def __init__(self, r: dict):
        self.r = r
        self.kind = r.get("kind", "contains")
        self.where = r.get("where", "any")
        self.action = r.get("action", "line")
        pat = r.get("pattern", "")
        flags = 0 if r.get("case") else re.I
        self.rx = None
        self.tkey = None
        if self.kind == "regex":
            try:
                self.rx = re.compile(pat, flags)
            except re.error:
                self.rx = None
        elif self.kind == "template":
            self.tkey = template_key(pat)
        else:
            esc = re.escape(pat)
            if self.kind == "exact":
                esc = r"^\s*" + esc + r"\s*$"
            elif self.kind == "startswith":
                esc = r"^\s*" + esc
            self.rx = re.compile(esc, flags) if pat else None
        self.label = f"правило «{pat}»"

    def applies_to(self, ln: Line, edge: int) -> bool:
        if self.where == "edges":
            return ln.pos_in_page < edge or ln.from_end < edge
        return True

    def matches(self, text: str) -> bool:
        if self.kind == "template":
            return bool(self.tkey) and template_key(text) == self.tkey
        return bool(self.rx and self.rx.search(text))

    def cut(self, text: str) -> str:
        if self.kind == "template" or not self.rx:
            return text
        return self.rx.sub(" ", text)


def compile_rules(cfg: dict) -> list[_CompiledRule]:
    return [_CompiledRule(r) for r in cfg.get("rules", [])
            if r.get("enabled", True) and r.get("pattern")]


def evaluate_lines(doc: Document, cfg: dict) -> list[LineStatus]:
    """Решение фильтра по каждой строке (без учёта ручных правок)."""
    rules = compile_rules(cfg)
    edge = int(cfg.get("edge_lines", 3))
    short = int(cfg.get("short_lines", 0) or 0)
    fn_lines = find_footnote_lines(doc) if cfg.get("footnote_lines", True) else set()
    out = []
    for li, ln in enumerate(doc.lines):
        t = ln.text
        st = LineStatus(True)
        for r in rules:
            if r.action == "line" and r.applies_to(ln, edge) and r.matches(t):
                st = LineStatus(False, r.label)
                break
        if st.included:
            if cfg.get("page_numbers", True) and PAGE_NUM_RE.match(t):
                st = LineStatus(False, "номер страницы")
            elif cfg.get("url_lines", True) and URL_RE.sub("", t).strip(" .,;:-–—()[]<>|") == "" \
                    and URL_RE.search(t):
                st = LineStatus(False, "ссылка")
            elif cfg.get("toc_lines", True) and TOC_RE.match(t):
                st = LineStatus(False, "строка оглавления")
            elif li in fn_lines:
                st = LineStatus(False, "сноска")
            elif cfg.get("no_letters", True) and not LETTER_RE.search(t):
                st = LineStatus(False, "нет букв")
            elif short and len(t) < short:
                st = LineStatus(False, f"короче {short} симв.")
        out.append(st)
    return out


def apply_inline_cuts(text: str, cfg: dict, rules: list[_CompiledRule] | None = None) -> str:
    if rules is None:
        rules = compile_rules(cfg)
    for r in rules:
        if r.action == "cut":
            text = r.cut(text)
    if cfg.get("url_inline", True):
        text = URL_RE.sub(" ", text)
    if cfg.get("footnote_marks", True):
        text = FOOTNOTE_RE.sub("", text)
    if cfg.get("square_brackets", False):
        text = re.sub(r"\[[^\[\]]*\]", " ", text)
    if cfg.get("parentheses", False):
        text = re.sub(r"\([^()]*\)", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# --------------------------------------------------------------------------
# автопоиск колонтитулов
# --------------------------------------------------------------------------
@dataclass
class HeaderCandidate:
    key: str
    example: str
    pages: int      # на скольких страницах (или сколько раз) встречается
    position: str   # "верх", "низ", "верх/низ", "каждые ~40 строк"
    where: str = "edges"   # зона для правила: edges — край страницы, any — где угодно


def detect_headers(doc: Document, edge: int = 3, min_ratio: float = 0.3) -> list[HeaderCandidate]:
    """Колонтитулы — строки, повторяющиеся вверху/внизу многих страниц.
    Если страниц нет (.doc, .txt, .rtf — весь текст одной «страницей») или по
    страницам ничего не нашлось — ищем строки, повторяющиеся через равные промежутки."""
    res = _detect_by_pages(doc, edge, min_ratio) if doc.page_count >= 3 else []
    return res or detect_periodic_lines(doc)


def _detect_by_pages(doc: Document, edge: int, min_ratio: float) -> list[HeaderCandidate]:
    pages_with: dict[str, set[int]] = defaultdict(set)
    example: dict[str, Counter] = defaultdict(Counter)
    pos: dict[str, set[str]] = defaultdict(set)
    for ln in doc.lines:
        top = ln.pos_in_page < edge
        bottom = ln.from_end < edge
        if not (top or bottom):
            continue
        k = template_key(ln.text)
        if len(k) < 2 or k in ("#",):
            continue
        pages_with[k].add(ln.page)
        example[k][ln.text] += 1
        pos[k].add("верх" if top else "низ")
    # «чётные/нечётные» колонтитулы встречаются на половине страниц
    need = max(2, math.ceil(doc.page_count * min_ratio))
    res = []
    for k, pg in pages_with.items():
        if len(pg) >= need:
            res.append(HeaderCandidate(k, example[k].most_common(1)[0][0], len(pg),
                                       "/".join(sorted(pos[k], reverse=True))))
    res.sort(key=lambda c: -c.pages)
    return res


def detect_periodic_lines(doc: Document, min_count: int = 3, min_gap: int = 8,
                          max_len: int = 120) -> list[HeaderCandidate]:
    """Колонтитулы без разбивки на страницы: короткая строка (цифры в ней любые),
    которая повторяется не меньше min_count раз примерно через одинаковое число строк.
    Регулярность отсекает обычные повторы — реплики в диалогах, «Статья», «Примечание»."""
    positions: dict[str, list[int]] = defaultdict(list)
    example: dict[str, Counter] = defaultdict(Counter)
    for i, ln in enumerate(doc.lines):
        t = ln.text.strip()
        if not t or len(t) > max_len:
            continue
        k = template_key(t)
        if len(k.replace("#", "").strip()) < 3:
            continue
        positions[k].append(i)
        example[k][t] += 1
    from .extract import is_chapter_heading
    res = []
    for k, pos in positions.items():
        if len(pos) < min_count:
            continue
        gaps = sorted(b - a for a, b in zip(pos, pos[1:]))
        med = gaps[len(gaps) // 2]
        if med < min_gap:
            continue
        regular = sum(1 for g in gaps if 0.6 * med <= g <= 1.5 * med)
        # колонтитул может пропадать на страницах с рисунками/таблицами — допускаем пропуски
        if regular < 0.7 * len(gaps):
            continue
        # «Глава 1», «Глава 2»… через равные промежутки — это заголовки, а не колонтитул
        texts = example[k]
        if len(texts) == len(pos) and all(is_chapter_heading(t) for t in texts):
            continue
        res.append(HeaderCandidate(k, texts.most_common(1)[0][0], len(pos),
                                   f"каждые ~{med} строк", where="any"))
    res.sort(key=lambda c: -c.pages)
    return res


# --------------------------------------------------------------------------
# сборка абзацев
# --------------------------------------------------------------------------
@dataclass
class Paragraph:
    text: str
    page: int = 0
    first_line: int = 0


_SENT_END = re.compile(r"[.!?…:;»\"”)\]]\s*$")
_HYPH_END = re.compile(r"[\w][\-­‐‑]$")
_MID_END = re.compile(r"(?:[^\W\d_]|[,–—-])\s*$")     # строка оборвалась на слове, запятой или тире
_OPEN_START = re.compile(r"^[«„\"“(\[]")
MIN_BROKEN_LINE = 50     # короче — может быть заголовком или строкой стихов


def _is_broken(prev: str, cur: str, t: str) -> bool:
    """Абзац t — продолжение предложения, оборванного концом страницы или лишним переносом
    абзаца (так бывает в DOCX, сделанных из PDF): prev — последняя строка, cur — весь
    собранный абзац."""
    from .extract import is_chapter_heading
    if _SENT_END.search(cur) or is_chapter_heading(t) or is_chapter_heading(prev):
        return False
    if t[:1].islower():
        return True
    # с заглавной, кавычки или числа: только если строка до разрыва длинная (не заголовок
    # и не стихи) и оборвалась посреди фразы
    if len(prev) < MIN_BROKEN_LINE or not _MID_END.search(prev):
        return False
    if _FN_LINE_RE.match(t) and not prev.rstrip()[-1:].isalpha():
        return False
    return t[:1].isupper() or bool(_OPEN_START.match(t)) or t[:1].isdigit()


def _join(a: str, b: str, fix_hyphen: bool) -> str:
    if not a:
        return b
    if fix_hyphen and _HYPH_END.search(a) and b[:1].islower():
        return a[:-1] + b
    if a.endswith("­"):
        return a[:-1] + b
    return a + " " + b


def build_paragraphs(doc: Document, included: list[bool], cfg: dict, opts: dict) -> list[Paragraph]:
    """opts: join_lines, fix_hyphen, merge_broken, max_para_chars"""
    rules = compile_rules(cfg)
    fn_nums = find_footnote_numbers(doc) if cfg.get("footnote_numbers", True) else {}
    join_lines = opts.get("join_lines", True)
    fix_hyphen = opts.get("fix_hyphen", True)
    merge_broken = opts.get("merge_broken", True)
    paras: list[Paragraph] = []
    cur = ""
    cur_page = 0
    cur_first = 0
    prev_li = -1
    prev_page = -1
    prev_text = ""
    # граница главы: абзац начинается с первой читаемой строки после неё
    ch_lines = sorted(c.line for c in getattr(doc, "chapters", []))
    ch_pos = 0
    for li, (ln, inc) in enumerate(zip(doc.lines, included)):
        chapter_break = False
        while ch_pos < len(ch_lines) and ch_lines[ch_pos] <= li:
            chapter_break = True
            ch_pos += 1
        if chapter_break:
            if cur:
                paras.append(Paragraph(cur, cur_page, cur_first))
                cur = ""
            prev_li = -1
        if not inc:
            continue
        prev_li, prev_page = li, ln.page
        t = ln.text
        if li in fn_nums:
            t = cut_spans(t, fn_nums[li])
        t = apply_inline_cuts(t, cfg, rules)
        if not t or not LETTER_RE.search(t) and not re.search(r"\d", t):
            continue
        new_para = ln.para_start or not join_lines
        if new_para and cur and merge_broken and join_lines and _is_broken(prev_text, cur, t):
            new_para = False
        if new_para and cur:
            paras.append(Paragraph(cur, cur_page, cur_first))
            cur = ""
        if not cur:
            cur_page = ln.page
            cur_first = li
        cur = _join(cur, t, fix_hyphen)
        prev_text = t
    if cur:
        paras.append(Paragraph(cur, cur_page, cur_first))
    # слишком длинные абзацы режем по предложениям (для навигации и подсветки)
    limit = int(opts.get("max_para_chars", 2500))
    out = []
    for p in paras:
        if len(p.text) <= limit:
            out.append(p)
            continue
        buf = ""
        for s in chunk_text(p.text, limit):
            if buf and len(buf) + len(s) + 1 > limit:
                out.append(Paragraph(buf, p.page, p.first_line))
                buf = s
            else:
                buf = (buf + " " + s).strip()
        if buf:
            out.append(Paragraph(buf, p.page, p.first_line))
    return out


# --------------------------------------------------------------------------
# главы
# --------------------------------------------------------------------------
MIN_CHAPTER_CHARS = 300   # глава короче — только заголовок; склеиваем с соседней


def _merge_small_chapters(starts: list[tuple[int, str]], lens: list[int]) -> list[tuple[int, str]]:
    """starts — (индекс абзаца, название); lens — длина каждого абзаца.
    Вступление перед первой главой и «пустые» главы (заголовок части прямо
    перед заголовком главы) приклеиваем к следующей главе, последнюю — к предыдущей."""
    if not starts:
        return []
    bounds = [s for s, _ in starts] + [len(lens)]
    size = [sum(lens[bounds[k]:bounds[k + 1]]) for k in range(len(starts))]
    out: list[list] = []
    carry = None          # (start, [названия]) мелкой главы, ждущей следующую
    for k, (s, title) in enumerate(starts):
        if carry is not None:
            s, titles = carry[0], carry[1] + [title]
            carry = None
        else:
            titles = [title]
        if size[k] < MIN_CHAPTER_CHARS and k + 1 < len(starts):
            carry = (s, titles)
            size[k + 1] += size[k]
            continue
        name = ". ".join(t.rstrip(". ") for t in titles) if len(titles) <= 2 else titles[-1]
        out.append([s, name])
    if len(out) > 1 and size[-1] < MIN_CHAPTER_CHARS:
        out.pop()     # крошечная последняя глава уходит в предыдущую
    return [(s, t) for s, t in out]


def _with_intro(starts: list[tuple[int, str]], lens: list[int], title: str) -> list[tuple[int, str]]:
    """Текст до первой главы: заметный — отдельная часть, короткий — в первую главу."""
    if starts and starts[0][0] != 0:
        if sum(lens[:starts[0][0]]) >= MIN_CHAPTER_CHARS:
            starts.insert(0, (0, title))
        else:
            starts[0] = (0, starts[0][1])
    return _merge_small_chapters(starts, lens)


def chapter_starts(doc: Document | None, paragraphs: list[Paragraph]) -> list[tuple[int, str]]:
    """Начала глав в списке абзацев документа: [(индекс абзаца, название)].
    Текст до первой главы относится к первой главе."""
    chs = sorted(getattr(doc, "chapters", []) or [], key=lambda c: c.line) if doc else []
    if not chs or not paragraphs:
        return []
    import bisect
    lines = [c.line for c in chs]
    starts: list[tuple[int, str]] = []
    last = None
    for i, p in enumerate(paragraphs):
        k = bisect.bisect_right(lines, p.first_line) - 1
        if k != last and k >= 0:
            starts.append((i, chs[k].title))
        last = k
    return _with_intro(starts, [len(p.text) for p in paragraphs], "Вступление")


def chapter_starts_from_texts(texts: list[str]) -> list[tuple[int, str]]:
    """То же для произвольного (отредактированного вручную) текста: абзацы-заголовки
    вида «Глава 5», «Часть вторая», «Пролог»."""
    from .extract import is_chapter_heading
    starts = [(i, t.strip()) for i, t in enumerate(texts) if is_chapter_heading(t)]
    if len(starts) < 2:
        return []
    return _with_intro(starts, [len(t) for t in texts], "Вступление")


def shift_chapters(starts: list[tuple[int, str]], first: int) -> list[tuple[int, str]]:
    """Главы для экспорта «с абзаца first»: индексы сдвигаются, текущая глава
    начинается с нулевого абзаца."""
    out = []
    for s, t in starts:
        if s <= first:
            out = [(0, t)]
        else:
            out.append((s - first, t))
    return out


# --------------------------------------------------------------------------
# предложения и чанки
# --------------------------------------------------------------------------
_ABBR_NO_SPLIT = r"(?<!\bт\.)(?<!\bг\.)(?<!\bгг\.)(?<!\bим\.)(?<!\bул\.)(?<!\bсм\.)(?<!\bрис\.)(?<!\bстр\.)(?<!\bтабл\.)(?<!\bMr\.)(?<!\bDr\.)(?<!\bMrs\.)(?<!\bSt\.)"
_SPLIT_RE = re.compile(r"(?<=[.!?…])" + _ABBR_NO_SPLIT + r"[\"»”')\]]*\s+(?=[\"«“'(\[—–-]?\s*[A-ZА-ЯЁ0-9])")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    # однобуквенные инициалы "А. С. Пушкин" — склеиваем
    out: list[str] = []
    for p in parts:
        if out and re.search(r"(?:^|\s)[A-ZА-ЯЁ]\.$", out[-1]):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Режет текст на куски по предложениям, каждый не длиннее max_chars."""
    chunks: list[str] = []
    for s in split_sentences(text):
        while len(s) > max_chars:
            cut = max(s.rfind(", ", 0, max_chars), s.rfind("; ", 0, max_chars),
                      s.rfind(" — ", 0, max_chars))
            if cut < max_chars // 3:
                cut = s.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            chunks.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            chunks.append(s)
    return chunks


# --------------------------------------------------------------------------
# нормализация перед синтезом
# --------------------------------------------------------------------------
ABBREVIATIONS_RU = [
    (r"\bт\.\s?е\.", "то есть"),
    (r"\bт\.\s?к\.", "так как"),
    (r"\bи\s+т\.\s?д\.", "и так далее"),
    (r"\bи\s+т\.\s?п\.", "и тому подобное"),
    (r"\bи\s+др\.", "и другие"),
    (r"\bи\s+пр\.", "и прочее"),
    (r"\bт\.\s?н\.", "так называемый"),
    (r"\bнапр\.", "например"),
    (r"\bсм\.", "смотри"),
    (r"\bср\.", "сравни"),
    (r"\bрис\.", "рисунок"),
    (r"\bтабл\.", "таблица"),
    (r"\bстр\.", "страница"),
    (r"\bгг\.", "годы"),
    (r"(?<=\d)\s?г\.", " год"),
    (r"(?<=\d)\s?вв?\.", " век"),
    (r"(?<=\d)\s?мин\.", " минут"),
    (r"(?<=\d)\s?сек\.", " секунд"),
    (r"\bим\.", "имени"),
    (r"\bул\.", "улица"),
    (r"\bтыс\.", "тысяч"),
    (r"\bмлн\.?(?=\s|$)", "миллионов"),
    (r"\bмлрд\.?(?=\s|$)", "миллиардов"),
    (r"\bруб\.", "рублей"),
    (r"\bкоп\.", "копеек"),
    (r"\bдоп\.", "дополнительно"),
    (r"\bприм\.", "примечание"),
    (r"\bред\.", "редакция"),
    (r"\bизд\.", "издание"),
    (r"\bт\.\s?ч\.", "том числе"),
    (r"(?<=\d)\s?км/ч", " километров в час"),
    (r"(?<=\d)\s?кг\b", " килограмм"),
    (r"(?<=\d)\s?км\b", " километров"),
    (r"(?<=\d)\s?см\b", " сантиметров"),
    (r"(?<=\d)\s?мм\b", " миллиметров"),
    (r"(?<=\d)\s?м\b", " метров"),
    (r"(?<=\d)\s?°C", " градусов Цельсия"),
]
_KEEP_DOT = {"и так далее", "и тому подобное", "и другие", "и прочее"}
_ABBR_COMPILED = [(re.compile(p, re.I if not p.startswith("(?<=\\d)") else 0), r) for p, r in ABBREVIATIONS_RU]

SYMBOLS_RU = {"№": " номер ", "§": " параграф ", "&": " и ", "°": " градусов ",
              "±": " плюс-минус ", "≈": " примерно ", "→": " — ", "×": " на ", "÷": " делить на ",
              "=": " равно ", "<": " меньше ", ">": " больше ", "≤": " меньше или равно ",
              "≥": " больше или равно ", "€": " евро ", "$": " долларов ", "₽": " рублей "}
SYMBOLS_EN = {"№": " number ", "§": " section ", "&": " and ", "°": " degrees ", "%": " percent ",
              "=": " equals ", "€": " euro ", "$": " dollars ", "<": " less than ", ">": " greater than "}


def is_cyrillic_text(text: str) -> bool:
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", text))
    lat = len(re.findall(r"[a-zA-Z]", text))
    return cyr >= lat


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _num_words(s: str, lang: str) -> str:
    from num2words import num2words
    s = s.replace(" ", "").replace(" ", "")
    try:
        if len(s.split(",")[0].split(".")[0]) > 15:
            return " ".join(num2words(int(c), lang=lang) for c in s if c.isdigit())
        if "," in s or "." in s:
            whole, frac = re.split(r"[.,]", s, 1)
            if lang == "ru":
                # "3,5" -> "три целых пять десятых" (num2words умеет float)
                if len(frac) <= 3:
                    return num2words(float(whole + "." + frac), lang="ru")
                return num2words(int(whole), lang="ru") + " запятая " + " ".join(
                    num2words(int(c), lang="ru") for c in frac)
            return num2words(float(whole + "." + frac), lang=lang)
        return num2words(int(s), lang=lang)
    except Exception:
        return s


_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d+))?(?![\w])(\s?%)?")
_DOTTED_RE = re.compile(r"\b\d+(?:\.\d+){2,}\b")


_GLUE_RE = re.compile(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def expand_numbers(text: str, lang: str) -> str:
    text = _GLUE_RE.sub(" ", text)   # «здесь1», «5G», «x2» -> разделяем
    text = _DOTTED_RE.sub(lambda m: (" точка " if lang == "ru" else " point ").join(
        _num_words(p, lang) for p in m.group(0).split(".")), text)

    def rep(m):
        whole, frac, pct = m.group(1), m.group(2), m.group(3)
        num = whole + ("," + frac if frac else "")
        words = _num_words(num, lang)
        if pct:
            if lang == "ru":
                if frac:
                    words += " процента"
                else:
                    words += " " + _plural_ru(int(whole.replace(" ", "").replace(" ", "")),
                                              "процент", "процента", "процентов")
            else:
                words += " percent"
        return words

    text = _NUM_RE.sub(rep, text)
    # диапазоны "1941–1945" после раскрытия: тире читаем как паузу
    return text


_TRANSLIT = [
    ("shch", "щ"), ("sch", "ш"), ("tion", "шн"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"),
    ("kh", "х"), ("ph", "ф"), ("th", "т"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"), ("yo", "ё"),
    ("ee", "и"), ("oo", "у"), ("ck", "к"), ("qu", "кв"), ("ay", "эй"), ("ey", "ей"), ("oy", "ой"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"),
    ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"),
    ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"), ("z", "з"),
]
_LETTER_NAMES = {"a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи",
                 "h": "эйч", "i": "ай", "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн",
                 "o": "оу", "p": "пи", "q": "кью", "r": "ар", "s": "эс", "t": "ти", "u": "ю",
                 "v": "ви", "w": "дабл-ю", "x": "икс", "y": "уай", "z": "зед"}


def translit_word(w: str) -> str:
    if len(w) == 1 or (w.isupper() and len(w) <= 5):   # буква/аббревиатура: по буквам
        return " ".join(_LETTER_NAMES.get(c.lower(), c) for c in w)
    s = w.lower()
    out = ""
    i = 0
    while i < len(s):
        for lat, cyr in _TRANSLIT:
            if s.startswith(lat, i):
                # "e" в конце слова обычно не читается
                if lat == "e" and i == len(s) - 1 and len(s) > 3:
                    cyr = ""
                if lat == "y" and i + 1 < len(s) and s[i + 1] in "aeiou":
                    cyr = "й"
                elif lat == "y" and i > 0:
                    cyr = "и"
                out += cyr
                i += len(lat)
                break
        else:
            out += s[i]
            i += 1
    return out


def transliterate_latin(text: str) -> str:
    return re.sub(r"[A-Za-z]+", lambda m: translit_word(m.group(0)), text)


_STRESS_VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
_ACUTE_RE = re.compile(f"([{_STRESS_VOWELS}])\u0301")
_PLUS_RE = re.compile(f"\\+([{_STRESS_VOWELS}])")


def apply_stress_style(text: str, style: str) -> str:
    """Ударения в одном виде для всех голосов. На входе «з+амок» или «за́мок»
    (знак над буквой). style: plus — «+» перед гласной (Silero); acute — знак над
    гласной (Piper/espeak; «+» он прочитал бы как «плюс»); none — убрать."""
    text = _ACUTE_RE.sub(r"+\1", text)
    if style == "plus":
        return text
    if style == "acute":
        # у «ё» ударение и так понятно
        return _PLUS_RE.sub(lambda m: m.group(1) if m.group(1) in "ёЁ" else m.group(1) + "\u0301", text)
    return _PLUS_RE.sub(r"\1", text)


def apply_replacements(text: str, replacements: list[dict]) -> str:
    for r in replacements or []:
        if not r.get("enabled", True) or not r.get("find"):
            continue
        flags = 0 if r.get("case") else re.I
        try:
            if r.get("regex"):
                text = re.sub(r["find"], r.get("replace", ""), text, flags=flags)
            else:
                pat = re.escape(r["find"])
                if r.get("whole_word", True) and re.match(r"\w", r["find"]) and re.search(r"\w$", r["find"]):
                    pat = r"\b" + pat + r"\b"
                text = re.sub(pat, lambda m: r.get("replace", ""), text, flags=flags)
        except re.error:
            continue
    return text


def expand_abbreviations(text: str) -> str:
    for rx, rep in _ABBR_COMPILED:
        def f(m, rep=rep):
            # если сокращение стояло в конце предложения — сохраняем точку
            end = m.end()
            tail = m.string[end:end + 3]
            if rep in _KEEP_DOT and re.match(r"\s+[А-ЯЁA-Z]", tail):
                return rep + "."
            return rep
        text = rx.sub(f, text)
    return text


def normalize_for_tts(text: str, opts: dict, caps: dict) -> str:
    """opts — настройки текста; caps — возможности движка:
    needs_numbers, latin_ok, allowed_chars (regex класса) """
    text = apply_replacements(text, opts.get("replacements", []))
    ru = is_cyrillic_text(text)
    if opts.get("abbreviations", True) and ru:
        text = expand_abbreviations(text)
    mode = opts.get("numbers", "auto")
    need_num = caps.get("needs_numbers", False) if mode == "auto" else mode == "on"
    if opts.get("symbols", True) or need_num:
        table = SYMBOLS_RU if ru else SYMBOLS_EN
        for k, v in table.items():
            if k in ("<", ">", "=") and not need_num:
                continue
            text = text.replace(k, v)
    if need_num:
        text = expand_numbers(text, "ru" if ru else "en")
    tmode = opts.get("translit", "auto")
    need_tr = (not caps.get("latin_ok", True)) if tmode == "auto" else tmode == "on"
    if need_tr:
        text = transliterate_latin(text)
    # типографика
    text = text.replace("…", "...").replace("­", "")
    text = re.sub(r"[«»“”„]", '"', text)
    text = re.sub(r"\s+", " ", text).strip()
    allowed = caps.get("allowed_chars")
    if allowed:
        text = re.sub(allowed, " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text
