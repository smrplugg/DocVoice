from __future__ import annotations

import threading

from ..log import log
from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


class FuncThread(QThread):
    """Запуск функции fn(progress, stop_flag, *args) в фоне."""
    progress = Signal(object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None, name: str = ""):
        super().__init__(parent)
        self.fn = fn
        self.args = args
        self.name = name or getattr(fn, "__name__", "задача")
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def cancelled(self) -> bool:
        return self._stop.is_set()

    def run(self):
        try:
            res = self.fn(lambda *a: self.progress.emit(a), self._stop.is_set, *self.args)
            self.done.emit(res)
        except InterruptedError as e:      # отмена пользователем — не ошибка
            log.info(f"«{self.name}»: отменено")
            self.failed.emit(str(e) or "Отменено")
        except Exception as e:
            log.exception(f"Ошибка: «{self.name}»")
            self.failed.emit(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)


def search_key(s: str) -> str:
    """Для поиска: без учёта регистра и разницы «е/ё», лишние пробелы не мешают."""
    return " ".join(s.lower().replace("ё", "е").split())


def is_dark() -> bool:
    pal = QApplication.palette()
    return pal.color(QPalette.Window).lightness() < 128


class LinesModel(QAbstractListModel):
    """Строки документа с отметками фильтра. Чекбокс — читать/не читать."""
    overridesChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self.status = []          # LineStatus
        self.overrides: dict[int, bool] = {}
        self.rows: list[tuple[str, int]] = []   # ("page", page) | ("line", idx)
        self.show_excluded = True
        self._row_by_line: dict[int, int] = {}
        self.search_q = ""        # подсвечиваем строки, где есть этот текст (см. search_key)

    def set_data(self, doc, status, overrides):
        self.beginResetModel()
        self.doc = doc
        self.status = status
        self.overrides = overrides
        self._rebuild_rows()
        self.endResetModel()

    def update_status(self, status):
        self.beginResetModel()
        self.status = status
        self._rebuild_rows()
        self.endResetModel()

    def set_search(self, q: str):
        self.search_q = search_key(q)
        if self.rows:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))

    def matches(self, row: int) -> bool:
        li = self.line_index(row)
        return bool(self.search_q) and li is not None and             self.search_q in search_key(self.doc.lines[li].text)

    def set_show_excluded(self, v: bool):
        self.beginResetModel()
        self.show_excluded = v
        self._rebuild_rows()
        self.endResetModel()

    def included(self, i: int) -> bool:
        if i in self.overrides:
            return self.overrides[i]
        return self.status[i].included if i < len(self.status) else True

    def _rebuild_rows(self):
        self.rows = []
        self._row_by_line = {}
        if not self.doc:
            return
        last_page = -1
        multi = self.doc.page_count > 1
        for i, ln in enumerate(self.doc.lines):
            if not self.show_excluded and not self.included(i):
                continue
            if multi and ln.page != last_page:
                self.rows.append(("page", ln.page))
                last_page = ln.page
            self.rows.append(("line", i))
        self._row_by_line = {v: r for r, (k, v) in enumerate(self.rows) if k == "line"}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def line_index(self, row: int) -> int | None:
        if 0 <= row < len(self.rows) and self.rows[row][0] == "line":
            return self.rows[row][1]
        return None

    def row_of_line(self, li: int) -> int | None:
        return self._row_by_line.get(li)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        kind, v = self.rows[index.row()]
        if kind == "page":
            if role == Qt.DisplayRole:
                return f"──────── {self.doc.page_label} {v + 1} ────────"
            if role == Qt.FontRole:
                f = QFont()
                f.setBold(True)
                return f
            if role == Qt.ForegroundRole:
                return QBrush(QColor("#7a8aa0"))
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            return None
        ln = self.doc.lines[v]
        inc = self.included(v)
        manual = v in self.overrides
        if role == Qt.DisplayRole:
            prefix = "¶ " if ln.para_start else "   "
            return prefix + ln.text
        if role == Qt.CheckStateRole:
            return Qt.Checked if inc else Qt.Unchecked
        if role == Qt.ForegroundRole and not inc:
            return QBrush(QColor("#8a8a8a"))
        if role == Qt.FontRole and not inc:
            f = QFont()
            f.setStrikeOut(True)
            return f
        if role == Qt.BackgroundRole and self.search_q and self.search_q in search_key(ln.text):
            return QBrush(QColor(110, 95, 20) if is_dark() else QColor(255, 225, 120))
        if role == Qt.BackgroundRole and manual:
            return QBrush(QColor(90, 80, 30) if is_dark() else QColor(255, 246, 200))
        if role == Qt.ToolTipRole:
            st = self.status[v]
            parts = [f"{self.doc.page_label} {ln.page + 1}, строка {ln.pos_in_page + 1}"]
            if manual:
                parts.append("Ручная отметка: " + ("читать" if inc else "не читать"))
                if st.reason:
                    parts.append(f"Фильтр: {st.reason}")
            elif not inc:
                parts.append(f"Исключено: {st.reason}")
            return "\n".join(parts)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        if self.rows[index.row()][0] == "page":
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole:
            return False
        li = self.line_index(index.row())
        if li is None:
            return False
        want = (Qt.CheckState(value) == Qt.Checked) if not isinstance(value, bool) else value
        self.set_override([li], want)
        return True

    def set_override(self, lines: list[int], want: bool | None):
        for li in lines:
            auto = self.status[li].included
            if want is None or want == auto:
                self.overrides.pop(li, None)
            else:
                self.overrides[li] = want
        if self.show_excluded:
            for li in lines:
                r = self.row_of_line(li)
                if r is not None:
                    idx = self.index(r)
                    self.dataChanged.emit(idx, idx)
        else:
            self.beginResetModel()
            self._rebuild_rows()
            self.endResetModel()
        self.overridesChanged.emit()
