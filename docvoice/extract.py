"""Извлечение текста из документов.

Результат — Document: список строк (Line) с номером страницы и признаком
начала нового абзаца. Строки потом фильтруются и собираются в абзацы.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {
    ".pdf": "PDF",
    ".docx": "Word",
    ".docm": "Word",
    ".doc": "Word 97-2003 (нужен Word или LibreOffice)",
    ".odt": "OpenDocument",
    ".rtf": "RTF",
    ".txt": "Текст",
    ".md": "Markdown",
    ".html": "HTML",
    ".htm": "HTML",
    ".xhtml": "HTML",
    ".epub": "EPUB",
    ".fb2": "FictionBook",
    ".zip": "FB2 в zip",
    ".pptx": "PowerPoint",
    ".djvu": "DjVu (нужен djvutxt)",
}

FILE_FILTER = (
    "Документы (" + " ".join("*" + e for e in SUPPORTED) + ");;"
    "Все файлы (*.*)"
)


@dataclass
class Line:
    page: int          # номер страницы/главы/слайда, с 0
    text: str
    para_start: bool   # строка начинает новый абзац
    pos_in_page: int = 0   # порядковый номер непустой строки на странице
    from_end: int = 0      # номер с конца страницы (0 — последняя)


@dataclass
class Chapter:
    title: str
    line: int          # индекс первой строки главы в Document.lines
    level: int = 1     # уровень заголовка (1 — верхний)


@dataclass
class Document:
    path: str
    lines: list[Line] = field(default_factory=list)
    page_count: int = 0
    page_label: str = "Страница"
    warnings: list[str] = field(default_factory=list)
    title: str = ""
    chapters: list[Chapter] = field(default_factory=list)


class ExtractError(Exception):
    pass


class Heading(str):
    """Строка-заголовок: экстракторы кладут её в страницы вместо обычной строки,
    build_document превращает её в Chapter."""
    level: int = 1

    def __new__(cls, text: str, level: int = 1):
        s = super().__new__(cls, text)
        s.level = level
        return s


# --------------------------------------------------------------------------
# сборка документа из "страниц"
# --------------------------------------------------------------------------
_WS = re.compile(r"[ \t\u00a0\u2000-\u200b\u202f\u205f\u3000]+")


def clean_line(s: str) -> str:
    s = s.replace("\ufeff", "").replace("\r", "")
    s = _WS.sub(" ", s)
    return s.strip()


def build_document(path: str, pages: list[list[str]], page_label: str = "Страница",
                   title: str = "") -> Document:
    """pages — список страниц, каждая — список строк; пустая строка = граница абзаца."""
    doc = Document(path=path, page_label=page_label, title=title)
    for pi, raw in enumerate(pages):
        page_lines: list[Line] = []
        brk = True
        for raw_s in raw:
            level = raw_s.level if isinstance(raw_s, Heading) else 0
            s = clean_line(raw_s)
            if not s:
                brk = True
                continue
            if level:
                title_ = clean_line(getattr(raw_s, "toc_title", "") or "") or s
                doc.chapters.append(Chapter(title_, len(doc.lines) + len(page_lines), level))
                brk = True
            page_lines.append(Line(pi, s, brk))
            brk = bool(level)   # после заголовка — новый абзац
        n = len(page_lines)
        for i, ln in enumerate(page_lines):
            ln.pos_in_page = i
            ln.from_end = n - 1 - i
        doc.lines.extend(page_lines)
    doc.page_count = len(pages)
    return doc


# --------------------------------------------------------------------------
# текстовые файлы
# --------------------------------------------------------------------------
def read_text_file(path: str) -> str:
    data = Path(path).read_bytes()
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data[:200000]).best()
        if best is not None:
            return data.decode(best.encoding, errors="replace")
    except Exception:
        pass
    return data.decode("cp1251", errors="replace")


_PARA_END = re.compile(r"[.!?…:;»\"”)]\s*$")


def _plain_pages(text: str, every_line: bool = False) -> list[list[str]]:
    """Разбивка простого текста на страницы (по \f) и абзацы.

    Абзацем считается: пустая строка; строка с отступом; конец предложения на
    заметно укороченной строке (для текстов с жёстким переносом); короткая строка
    без точки (заголовок) перед строкой с заглавной буквы. Если строки в среднем
    длинные (абзац = строка) или every_line=True — каждая строка отдельный абзац.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for p in text.split("\f"):
        lines = p.split("\n")
        nonempty = [l.rstrip() for l in lines if l.strip()]
        if not nonempty:
            out.append(lines)
            continue
        lens = sorted(len(l) for l in nonempty)
        median = lens[len(lens) // 2]
        width = lens[int(len(lens) * 0.9)]
        if every_line or median > 110:
            spaced = []
            for l in lines:
                spaced.extend([l, ""])
            out.append(spaced)
            continue
        res = []
        prev = ""
        for l in lines:
            s = l.rstrip()
            if not s.strip():
                res.append("")
                prev = ""
                continue
            if prev:
                brk = False
                if re.match(r"^(\t| {2,})\S", s):
                    brk = True
                elif _PARA_END.search(prev) and len(prev) < 0.8 * width:
                    brk = True
                elif len(prev.strip()) < 0.5 * width and not re.search(r"[,\-–—]$", prev) \
                        and s.strip()[:1].isupper():
                    brk = True
                elif re.match(r"^\s*([-•*–—]|\d+[.)])\s", s):
                    brk = True
                if brk:
                    res.append("")
            res.append(s)
            prev = s
        out.append(res)
    return out


def extract_txt(path: str) -> Document:
    return build_document(path, _plain_pages(read_text_file(path)), "Страница")


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")


def extract_md(path: str) -> Document:
    text = read_text_file(path)
    out = []
    in_code = False
    for l in text.replace("\r\n", "\n").split("\n"):
        if l.strip().startswith("```"):
            in_code = not in_code
            out.append("")
            continue
        if in_code:
            continue
        if re.match(r"^%\s", l):                       # заголовочный блок pandoc
            out.extend(["", l[1:].strip(), ""])
            continue
        l = re.sub(r"\[\^[^\]]+\]:?", "", l)               # сноски [^1]
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", l):
            out.append("")
            continue
        heading = re.match(r"^\s*(#{1,6})\s+", l)
        l = re.sub(r"^\s*#{1,6}\s+", "", l)
        l = re.sub(r"^\s*>\s?", "", l)
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", l):
            out.append("")
        l = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", l)
        l = _MD_LINK.sub(r"\1", l)
        l = re.sub(r"<[^>]+>", "", l)
        l = re.sub(r"(\*\*|__|\*|_|`|~~)", "", l)
        l = re.sub(r"^\s*\|", "", l).replace("|", ", ") if "|" in l else l
        if heading:
            out.extend(["", Heading(l, len(heading.group(1))), ""])
        else:
            out.append(l)
    return build_document(path, [out], "Страница")


# --------------------------------------------------------------------------
# HTML / EPUB / FB2
# --------------------------------------------------------------------------
_BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "td", "th",
               "blockquote", "pre", "section", "article", "header", "footer", "table",
               "ul", "ol", "dd", "dt", "figcaption", "title", "aside", "nav", "hr",
               "body", "main"]


def html_to_lines(html: str | bytes, skip_nav: bool = True) -> tuple[list[str], str]:
    import warnings
    from bs4 import BeautifulSoup
    try:
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except ImportError:
        pass
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    for t in soup(["script", "style", "noscript", "head", "svg", "math", "template"]):
        t.decompose()
    if skip_nav:
        for t in soup(["nav"]):
            t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for t in soup.find_all(_BLOCK_TAGS):
        t.insert_before("\n\n")
        t.insert_after("\n\n")
    for t in soup.find_all(["td", "th"]):
        t.insert_after(" ")
    for t in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if t.get_text(strip=True):
            t.insert(0, f"\x00H{t.name[1]}\x00")
    text = soup.get_text()
    lines = []
    pending = 0     # уровень заголовка, текст которого начнётся со следующей строки
    for l in text.split("\n"):
        m = _H_MARK.search(l)
        if m:
            pending = int(m.group(1))
            l = _H_MARK.sub("", l)
        if pending and l.strip():
            l = Heading(l, pending)
            pending = 0
        lines.append(l)
    return lines, title


_H_MARK = re.compile(r"\x00H(\d)\x00")


def extract_html(path: str) -> Document:
    data = Path(path).read_bytes()
    lines, title = html_to_lines(data)
    return build_document(path, [lines], "Страница", title)


def extract_epub(path: str) -> Document:
    """EPUB читаем как zip по spine, без внешних зависимостей кроме bs4."""
    import posixpath
    import xml.etree.ElementTree as ET

    pages: list[list[str]] = []
    title = ""
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = None
        for el in container.iter():
            if el.tag.endswith("rootfile"):
                rootfile = el.get("full-path")
                break
        if not rootfile:
            raise ExtractError("EPUB: не найден OPF")
        opf = ET.fromstring(z.read(rootfile))
        base = posixpath.dirname(rootfile)
        manifest = {}
        for el in opf.iter():
            tag = el.tag.split("}")[-1]
            if tag == "item":
                manifest[el.get("id")] = (el.get("href"), el.get("media-type", ""),
                                          el.get("properties", ""))
            elif tag == "title" and not title and el.text:
                title = el.text.strip()
        spine = [el.get("idref") for el in opf.iter() if el.tag.split("}")[-1] == "itemref"]
        names = set(z.namelist())
        for idref in spine:
            if idref not in manifest:
                continue
            href, mtype, props = manifest[idref]
            if "nav" in props.split():
                continue
            from urllib.parse import unquote
            full = posixpath.normpath(posixpath.join(base, unquote(href))) if base else unquote(href)
            if full not in names:
                continue
            lines, _ = html_to_lines(z.read(full))
            if any(l.strip() for l in lines):
                pages.append(lines)
    if not pages:
        raise ExtractError("EPUB: не найден текст")
    return build_document(path, pages, "Глава", title)


def _fb2_pages(data: bytes, skip_notes: bool = True) -> tuple[list[list[str]], str]:
    import xml.etree.ElementTree as ET
    # выкидываем объявление кодировки, если оно мешает
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("cp1251", errors="replace")
        text = re.sub(r"^<\?xml[^>]*\?>", "", text)
        root = ET.fromstring(text.encode("utf-8"))

    def local(tag):
        return tag.split("}")[-1] if isinstance(tag, str) else ""

    title = ""
    for el in root.iter():
        if local(el.tag) == "book-title" and el.text:
            title = el.text.strip()
            break

    pages: list[list[str]] = []

    def text_of(el) -> str:
        return "".join(el.itertext())

    def title_of(el) -> str:
        parts = [" ".join(text_of(p).split()) for p in el if local(p.tag) == "p"]
        parts = [p for p in parts if p]
        s = ""
        for p in parts:
            s = p if not s else s + (" " if re.search(r"[.!?…:;]$", s) else ". ") + p
        return s

    def walk(el, out: list[str], depth: int = 0):
        for ch in el:
            t = local(ch.tag)
            if t in ("p", "v", "subtitle", "text-author"):
                out.extend(["", text_of(ch)])
            elif t == "empty-line":
                out.append("")
            elif t == "title" and depth > 0 and title_of(ch):
                out.extend(["", Heading(title_of(ch), depth), ""])
            elif t in ("title", "epigraph", "poem", "stanza", "cite", "annotation"):
                out.append("")
                walk(ch, out, depth)
                out.append("")
            elif t == "section":
                walk(ch, out, depth + 1)
            elif t == "table":
                for row in ch:
                    out.extend(["", ", ".join(text_of(c).strip() for c in row)])

    for body in root:
        if local(body.tag) != "body":
            continue
        if skip_notes and body.get("name") in ("notes", "comments"):
            continue
        sections = [c for c in body if local(c.tag) == "section"]
        head: list[str] = []
        for c in body:
            if local(c.tag) in ("title", "epigraph"):
                walk(c, head)
                head.append("")
        if head:
            pages.append(head)
        if not sections:
            out: list[str] = []
            walk(body, out)
            pages.append(out)
        for s in sections:
            out = []
            walk(s, out, 1)
            pages.append(out)
    return pages, title


def extract_fb2(path: str) -> Document:
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
            if not names:
                raise ExtractError("В архиве нет .fb2")
            data = z.read(names[0])
    else:
        data = Path(path).read_bytes()
    pages, title = _fb2_pages(data)
    return build_document(path, pages, "Глава", title)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _pdf_order_blocks(blocks: list, page_width: float) -> list:
    """Порядок чтения с учётом колонок: полноширинные блоки делят страницу на
    полосы, внутри полосы сначала левая колонка сверху вниз, потом правая."""
    mid = page_width / 2
    tol = page_width * 0.03

    def kind(b):
        x0, x1 = b["bbox"][0], b["bbox"][2]
        if x1 <= mid + tol:
            return 0
        if x0 >= mid - tol:
            return 1
        return 2

    left = sum(1 for b in blocks if kind(b) == 0)
    right = sum(1 for b in blocks if kind(b) == 1)
    if left < 2 or right < 2:
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 0), b["bbox"][0]))
    out, band = [], []
    for b in sorted(blocks, key=lambda b: b["bbox"][1]):
        if kind(b) == 2:
            band.sort(key=lambda x: (kind(x), x["bbox"][1]))
            out.extend(band)
            band = []
            out.append(b)
        else:
            band.append(b)
    band.sort(key=lambda x: (kind(x), x["bbox"][1]))
    out.extend(band)
    return out


_PDF_SENT_END = re.compile(r"[.!?…:»\")]\s*$")


def _pdf_block_lines(block) -> list[str]:
    """Строки блока; пустая строка вставляется там, где по геометрии начинается
    новый абзац (короткая предыдущая строка с точкой или красная строка)."""
    lines = []
    for ln in block.get("lines", []):
        text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
        if text.strip():
            lines.append((ln["bbox"], text))
    if not lines:
        return []
    bx0, bx1 = block["bbox"][0], block["bbox"][2]
    width = max(1.0, bx1 - bx0)
    out = []
    prev = None
    for bbox, text in lines:
        if prev is not None:
            pb, pt = prev
            same_row = abs(bbox[1] - pb[1]) < 2  # спаны в одной строке
            if same_row:
                out[-1] += " " + text
                prev = (bbox, text)
                continue
            short_end = _PDF_SENT_END.search(pt) and pb[2] < bx1 - width * 0.12
            indent = bbox[0] > bx0 + max(8, width * 0.03) and pb[0] <= bx0 + 3
            if short_end or indent:
                out.append("")
        out.append(text)
        prev = (bbox, text)
    return out


def _pdf_page_lines(fitz, page) -> list[str]:
    """Строки страницы с порядком чтения по блокам; если разбор по блокам
    падает на «кривой» странице — простой текст."""
    try:
        flags = fitz.TEXT_PRESERVE_WHITESPACE | getattr(fitz, "TEXT_MEDIABOX_CLIP", 0)
        d = page.get_text("dict", flags=flags)
        blocks = [b for b in d.get("blocks", []) if b.get("type") == 0 and b.get("bbox")]
        lines: list[str] = []
        for b in _pdf_order_blocks(blocks, page.rect.width or 1):
            lines.append("")
            lines.extend(_pdf_block_lines(b))
        return lines
    except Exception:
        pass
    try:
        return _plain_pages(page.get_text("text") or "")[0]
    except Exception:
        return []


def _norm_title(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower().replace("ё", "е")).strip()


def _pdf_mark_toc(pages: list[list[str]], toc: list) -> None:
    """Пункты оглавления (закладок) PDF превращаем в заголовки: ищем строку
    с названием на нужной странице, иначе берём её первую строку."""
    for entry in toc:
        try:
            level, title, pno = int(entry[0]), str(entry[1]).strip(), int(entry[2]) - 1
        except (TypeError, ValueError, IndexError):
            continue
        if not title or not (0 <= pno < len(pages)):
            continue
        lines = pages[pno]
        key = _norm_title(title)
        idx = None
        for i, l in enumerate(lines):
            nl = _norm_title(l)
            if nl and (nl == key or key.startswith(nl) and len(nl) >= 4
                       or nl.startswith(key) and len(key) >= 4):
                idx = i
                break
        if idx is None:
            idx = next((i for i, l in enumerate(lines) if l.strip()), None)
        if idx is None:
            continue
        old = lines[idx]
        if isinstance(old, Heading) and old.level <= level:
            continue    # на эту строку уже указывает пункт того же или верхнего уровня
        h = Heading(str(old), level)
        h.toc_title = title   # в Chapter — название из оглавления, а не обрывок строки
        lines[idx] = h


def _clean_pdf_title(t: str) -> str:
    t = re.sub(r"^\s*Microsoft (?:Word|PowerPoint|Excel)\s*-\s*", "", t or "")
    return re.sub(r"\.(docx?|pptx?|xlsx?|odt|rtf)\s*$", "", t, flags=re.I).strip()


def extract_pdf(path: str) -> Document:
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # старые версии PyMuPDF
        except ImportError as e:
            raise ExtractError(f"Не установлен PyMuPDF (pip install pymupdf): {e}")
    pages: list[list[str]] = []
    empty_pages = 0
    image_pages = 0
    try:
        # файл читаем сами и отдаём MuPDF байты: путь с кириллицей некоторые
        # сборки PyMuPDF на Windows передают в fopen в неверной кодировке
        data = Path(path).read_bytes()
    except OSError as e:
        raise ExtractError(f"Не удалось открыть файл: {e}")
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ExtractError(f"PDF повреждён или не читается: {e}")
    with pdf:
        if pdf.needs_pass and not pdf.authenticate(""):
            raise ExtractError("PDF защищён паролем")
        for pno in range(pdf.page_count):
            try:
                page = pdf.load_page(pno)
            except Exception:
                pages.append([])
                empty_pages += 1
                continue
            lines = _pdf_page_lines(fitz, page)
            if not any(l.strip() for l in lines):
                empty_pages += 1
                try:
                    if page.get_images():
                        image_pages += 1
                except Exception:
                    pass
            pages.append(lines)
        try:
            title = (pdf.metadata or {}).get("title", "") or ""
        except Exception:
            title = ""
        try:
            toc = pdf.get_toc(simple=True) or []
        except Exception:
            toc = []
    _pdf_mark_toc(pages, toc)
    doc = build_document(path, pages, "Страница", _clean_pdf_title(title))
    if pages and empty_pages == len(pages) and image_pages:
        doc.warnings.append("В PDF нет текстового слоя (скан). Нужно предварительно "
                            "распознать текст (OCR), например в ABBYY или OCRmyPDF.")
    elif image_pages:
        doc.warnings.append(f"Страниц без текста (вероятно, сканы): {image_pages}. Они будут пропущены.")
    return doc


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def extract_docx(path: str, include_headers: bool = False) -> Document:
    import docx
    from docx.oxml.ns import qn

    try:
        d = docx.Document(path)
        body = d.element.body
    except Exception:
        # python-docx падает на битых служебных частях (колонтитулы, настройки) —
        # читаем основной текст прямо из word/document.xml
        from lxml import etree
        with zipfile.ZipFile(path) as z:
            root = etree.fromstring(z.read("word/document.xml"),
                                    etree.XMLParser(recover=True, huge_tree=True))
        d = None
        body = root.find(qn("w:body"))
        if body is None:
            raise
    pages: list[list[str]] = [[]]

    # уровни заголовков по стилям: «Heading 1» / «Заголовок 1» / уровень структуры
    style_level: dict[str, int] = {}
    try:
        for st in (d.styles if d is not None else []):
            el = st.element
            lvl = 0
            ppr = el.find(qn("w:pPr"))
            ol = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
            if ol is not None and (ol.get(qn("w:val")) or "").isdigit() and int(ol.get(qn("w:val"))) < 9:
                lvl = int(ol.get(qn("w:val"))) + 1
            else:
                m = re.match(r"^(?:heading|заголовок)\s*(\d)$", (st.name or "").strip(), re.I)
                if m:
                    lvl = int(m.group(1))
            if lvl and st.style_id:
                style_level[st.style_id] = lvl
    except Exception:
        pass

    def heading_level(p_el) -> int:
        ppr = p_el.find(qn("w:pPr"))
        if ppr is None:
            return 0
        ol = ppr.find(qn("w:outlineLvl"))
        if ol is not None and (ol.get(qn("w:val")) or "").isdigit():
            v = int(ol.get(qn("w:val")))
            return v + 1 if v < 9 else 0
        ps = ppr.find(qn("w:pStyle"))
        return style_level.get(ps.get(qn("w:val")), 0) if ps is not None else 0

    def para_text(p_el) -> tuple[str, bool]:
        """Текст абзаца и был ли внутри разрыв страницы перед текстом."""
        parts = []
        page_break = False
        for node in p_el.iter():
            tag = node.tag
            if tag == qn("w:t"):
                parts.append(node.text or "")
            elif tag == qn("w:tab"):
                parts.append(" ")
            elif tag in (qn("w:br"), qn("w:cr")):
                if node.get(qn("w:type")) == "page":
                    page_break = True
                else:
                    parts.append("\n")
            elif tag == qn("w:lastRenderedPageBreak"):
                if not "".join(parts).strip():
                    page_break = True
            elif tag == qn("w:footnoteReference") or tag == qn("w:endnoteReference"):
                pass
        return "".join(parts), page_break

    def add_para(p_el):
        txt, pb = para_text(p_el)
        if pb and any(l.strip() for l in pages[-1]):
            pages.append([])
        pages[-1].append("")
        lvl = heading_level(p_el)
        if lvl and txt.strip():
            pages[-1].append(Heading(" ".join(txt.split()), lvl))
        else:
            pages[-1].extend(txt.split("\n"))

    def walk(el):
        for ch in el.iterchildren():
            if ch.tag == qn("w:p"):
                ppr = ch.find(qn("w:pPr"))
                if ppr is not None and ppr.find(qn("w:pageBreakBefore")) is not None \
                        and any(l.strip() for l in pages[-1]):
                    pages.append([])
                add_para(ch)
            elif ch.tag == qn("w:tbl"):
                for row in ch.iter(qn("w:tr")):
                    cells = []
                    for cell in row.iter(qn("w:tc")):
                        ct = " ".join(para_text(p)[0] for p in cell.iter(qn("w:p"))).strip()
                        if ct:
                            cells.append(ct)
                    if cells:
                        pages[-1].extend(["", ", ".join(cells)])
            elif ch.tag == qn("w:sdt"):
                content = ch.find(qn("w:sdtContent"))
                if content is not None:
                    walk(content)

    walk(body)

    if include_headers and d is not None:
        hdr = []
        for s in d.sections:
            for part in (s.header, s.footer):
                for p in part.paragraphs:
                    if p.text.strip():
                        hdr.extend(["", p.text])
        if hdr:
            pages.insert(0, hdr)

    title = (d.core_properties.title or "") if d is not None else ""
    return build_document(path, pages, "Страница", title)


# --------------------------------------------------------------------------
# ODT
# --------------------------------------------------------------------------
def extract_odt(path: str) -> Document:
    import xml.etree.ElementTree as ET
    TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    pages: list[list[str]] = [[]]

    def inline(el) -> str:
        parts = [el.text or ""]
        for ch in el:
            t = ch.tag.split("}")[-1]
            if t == "s":
                parts.append(" " * int(ch.get(f"{{{TEXT}}}c", "1")))
            elif t == "tab":
                parts.append(" ")
            elif t == "line-break":
                parts.append("\n")
            elif t in ("note", "annotation"):
                pass
            else:
                parts.append(inline(ch))
            parts.append(ch.tail or "")
        return "".join(parts)

    def walk(el):
        for ch in el:
            t = ch.tag.split("}")[-1]
            if t == "h" and inline(ch).strip():
                lvl = ch.get(f"{{{TEXT}}}outline-level", "1")
                pages[-1].extend(["", Heading(" ".join(inline(ch).split()),
                                              int(lvl) if lvl.isdigit() else 1)])
            elif t in ("p", "h"):
                pages[-1].append("")
                pages[-1].extend(inline(ch).split("\n"))
            elif t == "soft-page-break":
                if any(l.strip() for l in pages[-1]):
                    pages.append([])
            elif t in ("table-row",):
                cells = []
                for c in ch:
                    ct = " ".join(inline(p) for p in c.iter() if p.tag.split("}")[-1] in ("p", "h")).strip()
                    if ct:
                        cells.append(ct)
                if cells:
                    pages[-1].extend(["", ", ".join(cells)])
            elif t in ("tracked-changes", "sequence-decls", "variable-decls"):
                continue
            else:
                walk(ch)

    walk(root)
    return build_document(path, pages, "Страница")


# --------------------------------------------------------------------------
# RTF
# --------------------------------------------------------------------------
def extract_rtf(path: str) -> Document:
    from striprtf.striprtf import rtf_to_text
    raw = Path(path).read_bytes().decode("latin-1")
    # кодировка: \ansicpgNNNN
    m = re.search(r"\\ansicpg(\d+)", raw)
    enc = f"cp{m.group(1)}" if m else "cp1251"
    try:
        text = rtf_to_text(raw, encoding=enc, errors="replace")
    except TypeError:
        text = rtf_to_text(raw)
    text = text.replace("\x0c", "\f")
    return build_document(path, _plain_pages(text, every_line=True), "Страница")


# --------------------------------------------------------------------------
# PPTX
# --------------------------------------------------------------------------
def extract_pptx(path: str, include_notes: bool = False) -> Document:
    from pptx import Presentation
    prs = Presentation(path)
    pages = []
    for slide in prs.slides:
        lines: list[str] = []
        shapes = sorted(slide.shapes, key=lambda s: ((s.top or 0), (s.left or 0)))
        for sh in shapes:
            if sh.has_text_frame:
                for p in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in p.runs)
                    lines.extend(["", t])
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        lines.extend(["", ", ".join(cells)])
        if include_notes and slide.has_notes_slide:
            nt = slide.notes_slide.notes_text_frame
            if nt is not None:
                lines.extend(["", nt.text])
        pages.append(lines)
    return build_document(path, pages, "Слайд")


# --------------------------------------------------------------------------
# DOC (Word 97-2003): собственный разбор формата, без Word.
# Запасной вариант — LibreOffice в отдельном процессе с таймаутом.
# --------------------------------------------------------------------------
def _doc_text_native(path: str) -> str:
    """Текст основного потока .doc по таблице кусков (piece table) из CLX."""
    import struct
    import olefile

    if not olefile.isOleFile(path):
        raise ExtractError("Файл не является документом Word 97-2003")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("WordDocument"):
            raise ExtractError("В файле нет потока WordDocument")
        wd = ole.openstream("WordDocument").read()
        ident, = struct.unpack_from("<H", wd, 0)
        if ident != 0xA5EC:
            raise ExtractError("Неподдерживаемая версия .doc (Word 6/95?)")
        flags, = struct.unpack_from("<H", wd, 0x0A)
        if flags & 0x0100:
            raise ExtractError("Документ защищён паролем")
        table_name = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_name):
            raise ExtractError("В файле нет таблицы " + table_name)
        table = ole.openstream(table_name).read()
    ccp_text, = struct.unpack_from("<i", wd, 0x004C)
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
    clx = table[fc_clx:fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:          # Prc — пропускаем
        cb, = struct.unpack_from("<h", clx, pos + 1)
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise ExtractError("Повреждённая таблица кусков .doc")
    lcb, = struct.unpack_from("<I", clx, pos + 1)
    plc = clx[pos + 5:pos + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts = []
    for i in range(n):
        cp0, cp1 = cps[i], cps[i + 1]
        if cp0 >= ccp_text:
            break
        cp1 = min(cp1, ccp_text)
        fc, = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)
        cnt = cp1 - cp0
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            parts.append(wd[off:off + cnt].decode("cp1252", errors="replace"))
        else:
            parts.append(wd[fc:fc + 2 * cnt].decode("utf-16-le", errors="replace"))
    text = "".join(parts)

    # поля: \x13 код \x14 результат \x15 — оставляем только результат
    out = []
    stack = []        # для каждого открытого поля: True, пока идёт код
    for ch in text:
        if ch == "\x13":
            stack.append(True)
            continue
        if ch == "\x14":
            if stack:
                stack[-1] = False
            continue
        if ch == "\x15":
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue
        out.append(ch)
    text = "".join(out)
    text = text.replace("\x07\x07", "\r").replace("\x07", ", ")      # таблицы
    text = text.replace("\x0b", "\n").replace("\x1e", "-").replace("\x1f", "")
    text = re.sub(r"[\x00-\x06\x08\x0e-\x12\x16-\x1d]", "", text)  # объекты, сноски
    return text


def _doc_pages_from_text(text: str) -> list[list[str]]:
    pages = []
    for page in text.split("\x0c"):
        lines = []
        for para in page.split("\r"):
            lines.append("")
            lines.extend(para.split("\n"))
        pages.append(lines)
    return pages


def _find_soffice() -> str | None:
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            p = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if p.exists():
                return str(p)
    return None


def _convert_with_soffice(src: str, outdir: str, fmt: str = "docx", timeout: int = 90) -> str | None:
    """Конвертация в отдельном процессе LibreOffice с собственным профилем
    (не конфликтует с открытым LibreOffice) и жёстким таймаутом."""
    so = _find_soffice()
    if not so:
        return None
    profile = Path(outdir) / "lo_profile"
    cmd = [so, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
           "--nolockcheck", "--convert-to", fmt, "--outdir", outdir, src]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None
    except Exception:
        return None
    out = Path(outdir) / (Path(src).stem + "." + fmt)
    return str(out) if out.exists() else None


def extract_doc(path: str) -> Document:
    err = None
    try:
        text = _doc_text_native(path)
        if text.strip():
            return build_document(path, _doc_pages_from_text(text), "Страница")
    except ExtractError as e:
        err = str(e)
        if "паролем" in err:
            raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    try:
        import olefile
        if not olefile.isOleFile(path):
            raise ExtractError("Файл повреждён или не является документом Word (.doc)")
    except ImportError:
        pass
    # запасной путь — LibreOffice
    with tempfile.TemporaryDirectory() as td:
        conv = _convert_with_soffice(path, td)
        if conv:
            doc = extract_docx(conv)
            doc.path = path
            return doc
    raise ExtractError(f"Не удалось прочитать .doc ({err}). Установите LibreOffice "
                       f"или пересохраните файл в .docx.")


def extract_djvu(path: str) -> Document:
    exe = shutil.which("djvutxt")
    if not exe:
        raise ExtractError("Для DjVu нужна утилита djvutxt (пакет DjVuLibre) в PATH.")
    res = subprocess.run([exe, path], capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = res.stdout.decode("utf-8", "replace")
    return build_document(path, _plain_pages(text), "Страница")


# --------------------------------------------------------------------------
EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".docm": extract_docx,
    ".doc": extract_doc,
    ".odt": extract_odt,
    ".rtf": extract_rtf,
    ".txt": extract_txt,
    ".md": extract_md,
    ".markdown": extract_md,
    ".html": extract_html,
    ".htm": extract_html,
    ".xhtml": extract_html,
    ".epub": extract_epub,
    ".fb2": extract_fb2,
    ".zip": extract_fb2,
    ".pptx": extract_pptx,
    ".djvu": extract_djvu,
}


# --------------------------------------------------------------------------
# главы
# --------------------------------------------------------------------------
_ORD = r"(?:перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат)\w*"
CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"(?:глава|часть|книга|раздел|chapter|part|book)\s*(?:\d{1,3}|[IVXLCDM]{1,7}|" + _ORD + r")\b"
    r"|(?:" + _ORD + r")\s+(?:глава|часть)\b"
    r"|(?:пролог|эпилог|предисловие|послесловие|вступление|prologue|epilogue|preface|afterword)\b"
    r")", re.I)
_CHAPTER_WORD = re.compile(r"^\s*(?:глава|chapter)\b", re.I)
_TOC_LINE = re.compile(r"(?:[.·…_]{4,}|\s{3,})\s*\d{1,4}\s*$")


def is_chapter_heading(text: str) -> bool:
    t = text.strip()
    if not (0 < len(t) <= 90) or _TOC_LINE.search(t):
        return False
    m = CHAPTER_RE.match(t)
    if not m:
        return False
    # «Глава 28 о следственном суде…», «Первая часть вести…» — это обычный текст
    rest = t[m.end():].lstrip()
    return not rest or not rest[0].isalpha() or rest[0].isupper()


def _guess_chapters(doc: Document) -> list[Chapter]:
    """Главы по строкам вида «Глава 5», «ЧАСТЬ ВТОРАЯ», «Chapter IV», «Пролог»."""
    res: list[Chapter] = []
    for i, ln in enumerate(doc.lines):
        if not ln.para_start or not is_chapter_heading(ln.text):
            continue
        # заголовок часто продолжается следующей строкой: «Глава 1» / «Начало»
        title = ln.text
        nxt = doc.lines[i + 1] if i + 1 < len(doc.lines) else None
        if nxt and nxt.page == ln.page and not nxt.para_start and len(title) + len(nxt.text) < 90:
            title += ". " + nxt.text
        # повторяющийся колонтитул «Глава 3» — берём только первое появление
        if res and _norm_title(res[-1].title) == _norm_title(title):
            continue
        res.append(Chapter(title, i, 1))
    return res if len(res) >= 2 else []


def _select_chapters(doc: Document) -> None:
    chs = doc.chapters
    if chs:
        by_level: dict[int, int] = {}
        for c in chs:
            by_level[c.level] = by_level.get(c.level, 0) + 1
        named = [c.level for c in chs if _CHAPTER_WORD.match(c.title)]
        if len(named) >= 2:
            level = max(set(named), key=named.count)
        else:
            level = next((lv for lv in sorted(by_level) if by_level[lv] >= 2), 0)
        doc.chapters = [c for c in chs if c.level <= level] if level else []
    if not doc.chapters and doc.page_label == "Глава" and doc.page_count > 1:
        # EPUB/FB2 без заголовков: глава — это файл/секция книги
        first: dict[int, int] = {}
        for i, ln in enumerate(doc.lines):
            first.setdefault(ln.page, i)
        doc.chapters = [Chapter(doc.lines[i].text, i, 1) for i in sorted(first.values())]
    if not doc.chapters:
        doc.chapters = _guess_chapters(doc)
    for c in doc.chapters:
        if len(c.title) > 120:
            c.title = c.title[:120].rsplit(" ", 1)[0] + "…"


def _sniff_ext(path: str, ext: str) -> str:
    """Файлы часто носят «чужое» расширение (rtf/html/docx под видом .doc)."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return ext
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0") and ext in (".docx", ".rtf", ".odt"):
        return ".doc"
    if head.startswith(b"PK") and ext in (".doc", ".rtf"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if "word/document.xml" in names:
                return ".docx"
            if "content.xml" in names:
                return ".odt"
        except Exception:
            pass
    if ext in (".doc", ".rtf") and re.search(rb"<(html|body|\?xml|w:document)", head.lower()):
        return ".html"
    return ext


def load_document(path: str) -> Document:
    ext = Path(path).suffix.lower()
    if ext == ".zip" and not path.lower().endswith(".fb2.zip"):
        with zipfile.ZipFile(path) as z:
            if not any(n.lower().endswith(".fb2") for n in z.namelist()):
                raise ExtractError("Zip-архив поддерживается только с книгой FB2 внутри")
    ext = _sniff_ext(path, ext)
    fn = EXTRACTORS.get(ext)
    if fn is None:
        # попробуем как текст
        try:
            return extract_txt(path)
        except Exception:
            raise ExtractError(f"Формат {ext} не поддерживается")
    try:
        doc = fn(path)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(f"Не удалось прочитать файл: {type(e).__name__}: {e}") from e
    if not doc.title:
        doc.title = Path(path).stem
    try:
        _select_chapters(doc)
    except Exception:
        doc.chapters = []
    if not doc.lines and not doc.warnings:
        doc.warnings.append("В документе не найден текст.")
    return doc
