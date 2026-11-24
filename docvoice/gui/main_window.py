"""Главное окно DocVoice."""
from __future__ import annotations

import copy
import os
import re
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QKeySequence, QPalette,
                           QTextCharFormat,
                           QTextCursor, QTextFormat)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider,
                               QSizePolicy, QSpinBox, QSplitter, QStyle, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import __version__, accent, ai, engines, textproc
from .. import log as applog
from ..log import describe_file, log
from ..audio import EXPORT_FORMATS, QUALITY_LEVELS, SPLIT_MODES, Player, export_audio
from ..engines import ENGINES
from ..extract import FILE_FILTER, ExtractError, load_document
from ..settings import Settings
from .ai_panel import AIPanel, AISettingsDialog
from .common import FuncThread, LinesModel, search_key
from .export_queue import ExportJob, ExportQueue, QueueWidget
from .voices_dialog import VoicesDialog

APP_NAME = "DocVoice"


def fmt_duration(sec: float) -> str:
    """«2 ч 15 мин», «40 мин», «меньше минуты» — без точности до секунд."""
    m = int(round(sec / 60))
    if sec and m == 0:
        return "меньше минуты"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


# ==========================================================================
# вспомогательные виджеты
# ==========================================================================
class LabeledSlider(QWidget):
    """Ползунок с подписью значения и кнопками «−»/«+» — по одному мелкому шагу
    (мелкий шаг мышью не поймать, а кнопкой — легко)."""

    def __init__(self, lo, hi, step, value, fmt="{:.2f}", parent=None):
        super().__init__(parent)
        self.k = 1 / step
        self.fmt = fmt
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(int(round(lo * self.k)), int(round(hi * self.k)))
        self.s.setSingleStep(1)          # стрелки на клавиатуре — один шаг
        self.s.setPageStep(5)            # PageUp/PageDown и щелчок мимо ручки — пять шагов
        self.s.setValue(int(round(value * self.k)))
        self.lbl = QLabel()
        self.lbl.setMinimumWidth(56)
        self.lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        minus, plus = QToolButton(), QToolButton()
        for b, d, tip in ((minus, -1, "Чуть меньше"), (plus, 1, "Чуть больше")):
            b.setText("−" if d < 0 else "+")
            b.setToolTip(tip)
            b.setAutoRepeat(True)        # если держать кнопку — значение меняется дальше
            b.clicked.connect(lambda _=False, d=d: self.s.setValue(self.s.value() + d))
        h.addWidget(minus)
        h.addWidget(self.s, 1)
        h.addWidget(plus)
        h.addWidget(self.lbl)
        self.s.valueChanged.connect(self._upd)
        self._upd()

    def _upd(self):
        self.lbl.setText(self.fmt.format(self.value()))

    def value(self) -> float:
        return self.s.value() / self.k

    def setValue(self, v):
        self.s.setValue(int(round(v * self.k)))

    @property
    def valueChanged(self):
        return self.s.valueChanged


class HeaderDialog(QDialog):
    """Список найденных колонтитулов с галочками."""

    def __init__(self, cands, page_count, label, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Найденные колонтитулы")
        self.resize(720, 420)
        v = QVBoxLayout(self)
        info = QLabel("Строки, которые повторяются на многих страницах (похоже на колонтитулы). "
                      "Отмеченные строки программа не будет читать. Цифры в них считаются "
                      "любыми — номера страниц не мешают.")
        info.setWordWrap(True)
        v.addWidget(info)
        self.t = QTableWidget(len(cands), 3)
        self.t.setHorizontalHeaderLabels(["Строка", "Сколько раз", "Где"])
        self.cands = cands
        self.t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.t.verticalHeader().setVisible(False)
        for r, c in enumerate(cands):
            it = QTableWidgetItem(c.example)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, r)
            self.t.setItem(r, 0, it)
            self.t.setItem(r, 1, QTableWidgetItem(str(c.pages)))
            self.t.setItem(r, 2, QTableWidgetItem(c.position))
        v.addWidget(self.t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Добавить отмеченные")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def chosen(self) -> list:
        return [self.cands[self.t.item(r, 0).data(Qt.UserRole)] for r in range(self.t.rowCount())
                if self.t.item(r, 0).checkState() == Qt.Checked]


class RuleDialog(QDialog):
    def __init__(self, rule: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Правило фильтра")
        self.resize(520, 200)
        f = QFormLayout(self)
        self.pat = QLineEdit(rule.get("pattern", ""))
        self.kind = QComboBox()
        for k, v in textproc.RULE_KINDS.items():
            self.kind.addItem(v, k)
        self.kind.setCurrentIndex(max(0, self.kind.findData(rule.get("kind", "contains"))))
        self.where = QComboBox()
        for k, v in textproc.RULE_WHERE.items():
            self.where.addItem(v, k)
        self.where.setCurrentIndex(max(0, self.where.findData(rule.get("where", "any"))))
        self.action = QComboBox()
        for k, v in textproc.RULE_ACTION.items():
            self.action.addItem(v, k)
        self.action.setCurrentIndex(max(0, self.action.findData(rule.get("action", "line"))))
        self.case = QCheckBox("Учитывать регистр")
        self.case.setChecked(rule.get("case", False))
        f.addRow("Текст / шаблон:", self.pat)
        f.addRow("Тип:", self.kind)
        f.addRow("Где искать:", self.where)
        f.addRow("Действие:", self.action)
        f.addRow("", self.case)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def rule(self) -> dict:
        return textproc.new_rule(self.pat.text(), self.kind.currentData(), self.where.currentData(),
                                 self.action.currentData(), self.case.isChecked())


# ==========================================================================
# главное окно
# ==========================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.doc = None
        self.status = []
        self.overrides: dict[int, bool] = {}
        self.paragraphs: list[textproc.Paragraph] = []
        self.play_paras: list[tuple[int, int, str]] = []   # (start, end, text) в редакторе
        self.manual_text = False
        self.player = Player()
        self.load_thread: FuncThread | None = None
        self._updating_ui = False

        self.setWindowTitle(APP_NAME)
        self.resize(1300, 820)
        self.setAcceptDrops(True)

        self.rebuild_timer = QTimer(self)
        self.rebuild_timer.setSingleShot(True)
        self.rebuild_timer.setInterval(250)
        self.rebuild_timer.timeout.connect(self.rebuild)

        self._base_font = QFont(QApplication.font())
        self._build_actions()
        self._build_ui()
        self._advanced_widgets += [self.chk_manual, self.btn_rebuild]
        self._load_settings_to_ui()
        self.apply_ui_scale()
        adv = bool(self.settings.data.get("advanced"))
        self.act_advanced.setChecked(adv)
        self.set_advanced(adv)
        if self.settings["text"].get("smart_accent", True):
            accent.preload_async()
        applog.set_gui_notifier(self._on_unhandled)

        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self._poll_player)
        self.poll.start()

        g = self.settings["geometry"]
        if g:
            try:
                self.restoreGeometry(QByteArray.fromBase64(g.encode()))
            except Exception:
                pass
        sp = self.settings["splitter"]
        if sp:
            try:
                self.splitter.restoreState(QByteArray.fromBase64(sp.encode()))
            except Exception:
                pass
        self._update_actions()

    # ------------------------------------------------------------------ UI
    def _build_actions(self):
        st = self.style()
        self.act_open = QAction(st.standardIcon(QStyle.SP_DialogOpenButton), "Открыть книгу", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.setToolTip("Открыть книгу или документ (Ctrl+O).\n"
                                 "Можно также перетащить файл мышкой прямо в окно.")
        self.act_open.triggered.connect(self.open_dialog)
        self.act_play = QAction(st.standardIcon(QStyle.SP_MediaPlay), "Читать", self)
        self.act_play.setShortcut(Qt.Key_F5)
        self.act_play.setToolTip("Начать или приостановить чтение вслух (F5 или пробел)")
        self.act_play.triggered.connect(self.play_pause)
        self.act_stop = QAction(st.standardIcon(QStyle.SP_MediaStop), "Стоп", self)
        self.act_stop.setShortcut(Qt.Key_F6)
        self.act_stop.setToolTip("Остановить чтение (F6)")
        self.act_stop.triggered.connect(self.stop)
        self.act_prev = QAction(st.standardIcon(QStyle.SP_MediaSeekBackward), "Назад", self)
        self.act_prev.setShortcut("Ctrl+Left")
        self.act_prev.setToolTip("Вернуться на абзац назад (Ctrl + стрелка влево)")
        self.act_prev.triggered.connect(lambda: self.jump(-1))
        self.act_next = QAction(st.standardIcon(QStyle.SP_MediaSeekForward), "Вперёд", self)
        self.act_next.setShortcut("Ctrl+Right")
        self.act_next.setToolTip("Перейти на абзац вперёд (Ctrl + стрелка вправо)")
        self.act_next.triggered.connect(lambda: self.jump(1))
        self.act_export = QAction(st.standardIcon(QStyle.SP_DialogSaveButton), "Сохранить аудио", self)
        self.act_export.setShortcut(QKeySequence.Save)
        self.act_export.setToolTip("Записать книгу в звуковой файл (MP3), чтобы слушать\n"
                                   "на телефоне или в плеере (Ctrl+S)")
        self.act_export.triggered.connect(self.export_dialog)
        self.act_voices = QAction(st.standardIcon(QStyle.SP_DriveNetIcon), "Голоса", self)
        self.act_voices.setToolTip("Скачать новые голоса или удалить ненужные")
        self.act_voices.triggered.connect(self.voices_dialog)
        self.act_font_minus = QAction("A−", self)
        self.act_font_minus.setToolTip("Сделать надписи мельче (Ctrl + минус)")
        self.act_font_minus.setShortcut("Ctrl+-")
        self.act_font_minus.triggered.connect(lambda: self.change_ui_scale(-25))
        self.act_font_plus = QAction("A+", self)
        self.act_font_plus.setToolTip("Сделать надписи крупнее (Ctrl + плюс)")
        self.act_font_plus.setShortcuts(["Ctrl++", "Ctrl+="])
        self.act_font_plus.triggered.connect(lambda: self.change_ui_scale(25))
        self.act_advanced = QAction(st.standardIcon(QStyle.SP_FileDialogDetailedView), "Все настройки", self)
        self.act_advanced.setCheckable(True)
        self.act_advanced.setToolTip("Показать дополнительные настройки: очистку текста, произношение,\n"
                                     "паузы и прочее. Обычно они не нужны.")
        self.act_advanced.toggled.connect(self.set_advanced)

        tb = QToolBar("Основное")
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setMovable(False)
        self.toolbar = tb
        self.addToolBar(tb)
        tb.addAction(self.act_open)
        self.recent_btn = QPushButton("Недавние ▾")
        self.recent_btn.setFlat(True)
        self.recent_btn.setToolTip("Книги, которые вы открывали раньше")
        self.recent_menu = QMenu(self)
        self.recent_btn.setMenu(self.recent_menu)
        tb.addWidget(self.recent_btn)
        tb.addSeparator()
        tb.addAction(self.act_prev)
        tb.addAction(self.act_play)
        tb.addAction(self.act_stop)
        tb.addAction(self.act_next)
        tb.addSeparator()
        tb.addAction(self.act_export)
        tb.addAction(self.act_voices)
        tb.addSeparator()
        self.now_label = QLabel("")
        self.now_label.setContentsMargins(8, 0, 8, 0)
        tb.addWidget(self.now_label)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_font_minus)
        tb.addAction(self.act_font_plus)
        tb.addAction(self.act_advanced)
        self.help_btn = QToolButton()
        self.help_btn.setText("Помощь")
        self.help_btn.setIcon(st.standardIcon(QStyle.SP_DialogHelpButton))
        self.help_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.help_btn.setPopupMode(QToolButton.InstantPopup)
        self.help_btn.setToolTip("Руководство, проверка программы, отчёт о проблеме")
        hm = QMenu(self.help_btn)
        hm.addAction("Связь с разработчиком…", self.contact_developer)
        hm.addSeparator()
        hm.addAction("Открыть руководство", lambda: self._open_doc_file("Руководство.txt"))
        hm.addAction("Открыть краткую инструкцию", lambda: self._open_doc_file("Инструкция.txt"))
        hm.addSeparator()
        hm.addAction("Проверить, всё ли работает…", self.run_selftest)
        # для себя: помощник на нейросети; пункт виден в режиме «Все настройки» или когда ключ уже введён
        self.act_ai = hm.addAction("Нейросеть (помощник)…", self.ai_settings)
        hm.addAction("Сохранить отчёт о проблеме…", self.save_report)
        hm.addAction("Открыть папку с журналом", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(applog.logs_dir()))))
        hm.addSeparator()
        hm.addAction("О программе", self._about)
        self.help_btn.setMenu(hm)
        tb.addWidget(self.help_btn)

    def _build_ui(self):
        self.splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(self.splitter)

        # ---- левая часть: текст книги, строки, сохранение
        self.left_tabs = QTabWidget()
        self.splitter.addWidget(self.left_tabs)

        text_w = QWidget()
        self.text_tab = text_w
        tv = QVBoxLayout(text_w)
        tv.setContentsMargins(4, 4, 4, 4)
        tbar = QHBoxLayout()
        self.chk_follow = QCheckBox("Показывать место, которое читается")
        tbar.addWidget(self.chk_follow)
        tbar.addStretch(1)
        self.chk_manual = QCheckBox("Разрешить правку текста")
        self.chk_manual.setToolTip("Включите, чтобы исправить текст перед чтением. Пока включено, "
                                   "изменения на вкладках «Очистка текста» и «Что читать» не применяются.")
        self.chk_manual.toggled.connect(self._on_manual_toggled)
        tbar.addWidget(self.chk_manual)
        self.btn_rebuild = QPushButton("Вернуть текст из книги")
        self.btn_rebuild.setToolTip("Отменить ручные исправления и заново взять текст из книги")
        self.btn_rebuild.clicked.connect(self._force_rebuild)
        tbar.addWidget(self.btn_rebuild)
        tv.addLayout(tbar)
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText("Нажмите «Открыть книгу» вверху слева\n"
                                       "или перетащите файл в это окно мышкой.\n\n"
                                       "Подходят книги и документы: PDF, Word (DOC, DOCX), EPUB, FB2, "
                                       "TXT, RTF, ODT, HTML, PowerPoint, DjVu.")
        self.editor.textChanged.connect(self._on_editor_changed)
        self.editor.installEventFilter(self)   # пробел — пауза, даже если щёлкнули в текст
        tv.addWidget(self.editor, 1)
        self.left_tabs.addTab(text_w, "Текст книги")

        lines_w = QWidget()
        self.lines_tab = lines_w
        lv = QVBoxLayout(lines_w)
        lv.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти слово…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Начните печатать — подходящие строки подсветятся жёлтым.\n"
                               "Enter или F3 — следующая найденная строка.")
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(lambda: self.find_next(1))
        bar.addWidget(self.search, 1)
        self.btn_find_prev = QPushButton("◀ Предыдущее")
        self.btn_find_prev.clicked.connect(lambda: self.find_next(-1))
        bar.addWidget(self.btn_find_prev)
        self.btn_find_next = QPushButton("Следующее ▶")
        self.btn_find_next.clicked.connect(lambda: self.find_next(1))
        bar.addWidget(self.btn_find_next)
        self.lbl_found = QLabel("")
        self.lbl_found.setMinimumWidth(90)
        bar.addWidget(self.lbl_found)
        lv.addLayout(bar)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._apply_search)
        self.chk_show_excl = QCheckBox("Показывать строки, которые не будут читаться")
        self.chk_show_excl.toggled.connect(self._on_show_excluded)
        lv.addWidget(self.chk_show_excl)
        self.lines_model = LinesModel(self)
        self.lines_model.overridesChanged.connect(self.schedule_rebuild)
        self.lines_view = QListView()
        self.lines_view.setModel(self.lines_model)
        self.lines_view.setUniformItemSizes(True)
        self.lines_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lines_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lines_view.customContextMenuRequested.connect(self._lines_menu)
        self.lines_view.doubleClicked.connect(self._line_double_clicked)
        # выделенная строка хорошо видна, даже когда курсор в поле поиска
        pal = self.lines_view.palette()
        for role in (QPalette.Highlight, QPalette.HighlightedText):
            pal.setColor(QPalette.Inactive, role, pal.color(QPalette.Active, role))
        self.lines_view.setPalette(pal)
        lv.addWidget(self.lines_view, 1)
        hint = QLabel("Галочка стоит — строка читается. Снимите галочку, чтобы строку пропустить.\n"
                      "Правая кнопка мыши — пропустить все похожие строки или читать с этого места.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lv.addWidget(hint)
        self.left_tabs.addTab(lines_w, "Что читать")

        self.queue = ExportQueue(self)
        for k, v in (self.settings.data.get("synth_rates") or {}).items():
            eid, _, vid = k.partition("|")
            self.queue.rates[(eid, vid)] = v
        self.queue_widget = QueueWidget(self.queue)
        self.left_tabs.addTab(self.queue_widget, "Сохранение")
        self.queue.changed.connect(self._on_queue_changed)
        self.queue.jobFinished.connect(self._on_job_finished)
        self.queue.allFinished.connect(self._on_queue_all_finished)
        self.queue_status_timer = QTimer(self)
        self.queue_status_timer.setInterval(1000)
        self.queue_status_timer.timeout.connect(self._update_queue_status)
        self.queue_status_timer.start()
        self.left_tabs.setCurrentWidget(self.text_tab)

        # ---- правая часть: настройки
        self.right_tabs = QTabWidget()
        self.voice_tab = self._scroll(self._voice_panel())
        self.export_tab = self._scroll(self._export_panel())
        self.filter_tab = self._scroll(self._filter_panel())
        self.textopts_tab = self._scroll(self._text_panel())
        self.right_tabs.addTab(self.voice_tab, "Голос")
        self.right_tabs.addTab(self.export_tab, "Сохранение в файл")
        self.right_tabs.addTab(self.filter_tab, "Очистка текста")
        self.right_tabs.addTab(self.textopts_tab, "Произношение")
        self.ai_tab = AIPanel(self)
        self.right_tabs.addTab(self.ai_tab, "Помощник")
        self.splitter.addWidget(self.right_tabs)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([800, 480])

        # ---- статус
        self.sb_doc = QLabel("")
        self.sb_queue = QLabel("")
        self.statusBar().addWidget(self.sb_doc, 1)
        self.statusBar().addPermanentWidget(self.sb_queue)

        # ---- оценка длительности: считается с задержкой, замер скорости голоса — в фоне
        self.estimate_timer = QTimer(self)
        self.estimate_timer.setSingleShot(True)
        self.estimate_timer.setInterval(400)
        self.estimate_timer.timeout.connect(self._do_update_estimate)
        self.calib_timer = QTimer(self)
        self.calib_timer.setSingleShot(True)
        self.calib_timer.setInterval(1500)
        self.calib_timer.timeout.connect(self._calibrate_voice)
        self.calib_thread: FuncThread | None = None

    # ---------------------------------------------------- простой режим, шрифт
    def set_advanced(self, on: bool):
        """Простой режим прячет всё, что не нужно, чтобы открыть книгу, послушать её
        и сохранить в MP3."""
        self.settings["advanced"] = bool(on)
        self.settings.save()
        for tabs, w in ((self.left_tabs, self.lines_tab), (self.right_tabs, self.filter_tab),
                        (self.right_tabs, self.textopts_tab)):
            tabs.setTabVisible(tabs.indexOf(w), on)
        for w in self._advanced_widgets:
            w.setVisible(on)
        for form, w in self._advanced_rows:
            form.setRowVisible(w, on)
            form.invalidate()
        self._on_engine_changed_visibility()
        self._update_ai_visibility()

    def _update_ai_visibility(self):
        has_key = ai.enabled(self.settings)
        self.right_tabs.setTabVisible(self.right_tabs.indexOf(self.ai_tab), has_key)
        self.act_ai.setVisible(has_key or bool(self.settings.data.get("advanced")))
        self.ai_tab.refresh()

    def ai_settings(self):
        had = ai.enabled(self.settings)
        if AISettingsDialog(self.settings, self).exec() == QDialog.Accepted:
            self._update_ai_visibility()
            if ai.enabled(self.settings) and not had:
                self.right_tabs.setCurrentWidget(self.ai_tab)

    def apply_ui_scale(self):
        scale = int(self.settings.data.get("ui_scale") or 100)
        f = QFont(self._base_font)
        f.setPointSizeF(self._base_font.pointSizeF() * scale / 100)
        QApplication.instance().setFont(f)
        # виджеты со своим стилем (серые подсказки) сами шрифт не обновляют
        for wdg in self.findChildren(QWidget):
            if wdg.styleSheet():
                wdg.setFont(f)
                wdg.setStyleSheet(wdg.styleSheet())
        ef = QFont(f)
        ef.setPointSizeF(f.pointSizeF() * 1.25)
        self.editor.setFont(ef)
        icon = int(26 * scale / 100)
        self.toolbar.setIconSize(QSize(icon, icon))
        self.act_font_minus.setEnabled(scale > 75)
        self.act_font_plus.setEnabled(scale < 250)

    def change_ui_scale(self, delta: int):
        scale = int(self.settings.data.get("ui_scale") or 100)
        self.settings["ui_scale"] = max(75, min(250, scale + delta))
        self.settings.save()
        self.apply_ui_scale()
        self.statusBar().showMessage(f"Размер надписей: {self.settings['ui_scale']}%", 3000)

    def _scroll(self, w):
        # длинные пункты списков не должны растягивать панель вширь
        for cb in w.findChildren(QComboBox):
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(10)
        # при крупном шрифте подпись переносится над полем, а не обрезается
        for form in w.findChildren(QFormLayout):
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        sa = QScrollArea()
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidgetResizable(True)
        sa.setWidget(w)
        sa.setFrameShape(QScrollArea.NoFrame)
        return sa

    # ---- панель «Голос»
    def _voice_panel(self):
        self._advanced_widgets: list[QWidget] = []
        self._advanced_rows: list[tuple[QFormLayout, QWidget]] = []
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Кто читает")
        f = QFormLayout(g)
        self.cb_engine = QComboBox()
        for eid, cls in ENGINES.items():
            ok, msg = cls.available()
            self.cb_engine.addItem(cls.title + ("" if ok else "  (не установлено)"), eid)
        self.cb_engine.currentIndexChanged.connect(self._on_engine_changed)
        f.addRow("Набор голосов:", self.cb_engine)
        self.lbl_engine = QLabel()
        self.lbl_engine.setWordWrap(True)
        self.lbl_engine.setStyleSheet("color: gray;")
        f.addRow(self.lbl_engine)
        vrow = QHBoxLayout()
        self.cb_voice = QComboBox()
        self.cb_voice.setMinimumWidth(200)
        self.cb_voice.currentIndexChanged.connect(self._on_voice_changed)
        vrow.addWidget(self.cb_voice, 1)
        b = QPushButton("Скачать ещё…")
        b.setToolTip("Скачать другие голоса из интернета")
        b.clicked.connect(self.voices_dialog)
        vrow.addWidget(b)
        f.addRow("Голос:", vrow)
        trow = QHBoxLayout()
        self.ed_test = QLineEdit()
        self.ed_test.setToolTip("Можно написать свою фразу и послушать, как она звучит")
        trow.addWidget(self.ed_test, 1)
        self.btn_test = QPushButton("▶ Послушать")
        self.btn_test.clicked.connect(self.test_voice)
        trow.addWidget(self.btn_test)
        f.addRow("Проба голоса:", trow)
        self.accent_hint = QWidget()
        hrow = QHBoxLayout(self.accent_hint)
        hrow.setContentsMargins(0, 0, 0, 0)
        hl = QLabel("Голос ошибается в ударениях?")
        hl.setWordWrap(True)
        hrow.addWidget(hl, 1)
        hb = QPushButton("Скачать умные ударения…")
        hb.setToolTip("Программа сама будет ставить ударения и букву «ё» по смыслу предложения")
        hb.clicked.connect(lambda: self.voices_dialog(tab="accent"))
        hrow.addWidget(hb)
        f.addRow(self.accent_hint)
        v.addWidget(g)

        g = QGroupBox("Как читать")
        f = QFormLayout(g)
        self.sl_speed = LabeledSlider(0.5, 2.5, 0.01, 1.0, "×{:.2f}")
        self.sl_speed.setToolTip("×1 — обычная скорость. Меньше — медленнее, больше — быстрее.\n"
                                 "Кнопки − и + меняют по чуть-чуть.")
        f.addRow("Скорость:", self.sl_speed)
        self.sl_volume = LabeledSlider(0.1, 2.0, 0.01, 1.0, "{:.0%}")
        f.addRow("Громкость:", self.sl_volume)
        self.sl_pitch = LabeledSlider(-1.0, 1.0, 0.02, 0.0, "{:+.2f}")
        self.sl_pitch.setToolTip("Выше или ниже голос. Работает не у всех голосов.")
        f.addRow("Высота голоса:", self.sl_pitch)
        self._advanced_rows.append((f, self.sl_pitch))
        self.sp_sent = QSpinBox()
        self.sp_sent.setRange(0, 3000)
        self.sp_sent.setSingleStep(50)
        self.sp_sent.setSuffix(" мс")
        self.sp_sent.setSpecialValueText("обычная")
        self.sp_sent.setToolTip("1000 мс = 1 секунда")
        f.addRow("Пауза после предложения:", self.sp_sent)
        self._advanced_rows.append((f, self.sp_sent))
        self.sp_para = QSpinBox()
        self.sp_para.setRange(0, 5000)
        self.sp_para.setSingleStep(100)
        self.sp_para.setSuffix(" мс")
        self.sp_para.setToolTip("1000 мс = 1 секунда")
        f.addRow("Пауза после абзаца:", self.sp_para)
        self._advanced_rows.append((f, self.sp_para))
        self.chk_trim = QCheckBox("Убирать лишнюю тишину внутри фраз")
        f.addRow(self.chk_trim)
        self._advanced_rows.append((f, self.chk_trim))
        v.addWidget(g)

        self.g_piper = QGroupBox("Голоса Piper — тонкая настройка")
        f = QFormLayout(self.g_piper)
        self.sp_noise = QDoubleSpinBox()
        self.sp_noise.setRange(0, 1.5)
        self.sp_noise.setSingleStep(0.05)
        self.sp_noise.setSpecialValueText("обычная")
        self.sp_noise.setToolTip("Насколько живо меняется интонация (обычно 0.667). 0 — как задумано голосом.")
        f.addRow("Живость интонации:", self.sp_noise)
        self.sp_noisew = QDoubleSpinBox()
        self.sp_noisew.setRange(0, 1.5)
        self.sp_noisew.setSingleStep(0.05)
        self.sp_noisew.setSpecialValueText("обычная")
        self.sp_noisew.setToolTip("Насколько разной длины звуки (обычно 0.8). 0 — как задумано голосом.")
        f.addRow("Неровность ритма:", self.sp_noisew)
        v.addWidget(self.g_piper)

        self.g_silero = QGroupBox("Голоса Silero — тонкая настройка")
        f = QFormLayout(self.g_silero)
        self.cb_silero_sr = QComboBox()
        for sr, name in ((48000, "Высокое"), (24000, "Среднее"), (8000, "Низкое (как по телефону)")):
            self.cb_silero_sr.addItem(name, sr)
        f.addRow("Качество звука:", self.cb_silero_sr)
        self.chk_accent = QCheckBox("Самостоятельно ставить ударения")
        self.chk_yo = QCheckBox("Самостоятельно расставлять букву «ё»")
        f.addRow(self.chk_accent)
        f.addRow(self.chk_yo)
        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(0, 64)
        self.sp_threads.setSpecialValueText("сколько есть")
        self.sp_threads.setToolTip("Сколько ядер процессора занимать. Меньше — компьютер меньше "
                                   "тормозит, но озвучка медленнее.")
        f.addRow("Ядер процессора:", self.sp_threads)
        v.addWidget(self.g_silero)

        g = QGroupBox("Куда выводить звук")
        f = QFormLayout(g)
        self.cb_device = QComboBox()
        self._fill_devices()
        f.addRow("Колонки или наушники:", self.cb_device)
        v.addWidget(g)
        self._advanced_widgets.append(g)
        v.addStretch(1)

        for wdg in (self.sl_speed, self.sl_pitch, self.sl_volume):
            wdg.valueChanged.connect(self._save_params)
        for wdg in (self.sp_sent, self.sp_para, self.sp_threads):
            wdg.valueChanged.connect(self._save_params)
        for wdg in (self.sp_noise, self.sp_noisew):
            wdg.valueChanged.connect(self._save_params)
        for wdg in (self.chk_trim, self.chk_accent, self.chk_yo):
            wdg.toggled.connect(self._save_params)
        self.cb_silero_sr.currentIndexChanged.connect(self._save_params)
        self.cb_device.currentIndexChanged.connect(self._save_params)
        return w

    def _on_engine_changed_visibility(self):
        adv = bool(self.settings.data.get("advanced"))
        eid = self.cb_engine.currentData()
        self.accent_hint.setVisible(ENGINES[eid].stress_style != "none" and not accent.installed())
        self.g_piper.setVisible(adv and eid == "piper")
        self.g_silero.setVisible(adv and eid == "silero")

    def _fill_devices(self):
        self.cb_device.clear()
        self.cb_device.addItem("По умолчанию", None)
        try:
            import sounddevice as sd
            hostapis = sd.query_hostapis()
            default_api = sd.default.hostapi
            for i, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0 and d["hostapi"] == default_api:
                    self.cb_device.addItem(d["name"], i)
        except Exception:
            pass

    # ---- панель «Фильтр»
    def _filter_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)

        g = QGroupBox("Профиль фильтра")
        h = QHBoxLayout(g)
        self.cb_profile = QComboBox()
        self.cb_profile.setMinimumWidth(160)
        h.addWidget(self.cb_profile, 1)
        b_load = QPushButton("Применить")
        b_load.clicked.connect(self._profile_load)
        b_save = QPushButton("Сохранить как…")
        b_save.clicked.connect(self._profile_save)
        b_del = QPushButton("Удалить")
        b_del.clicked.connect(self._profile_delete)
        for b in (b_load, b_save, b_del):
            h.addWidget(b)
        v.addWidget(g)

        g = QGroupBox("Автоматически пропускать")
        f = QVBoxLayout(g)
        self.chk_f = {}
        for key, text in (("page_numbers", "Номера страниц («12», «- 12 -», «стр. 5», «5 из 120»)"),
                          ("url_lines", "Строки, состоящие только из ссылки или e-mail"),
                          ("url_inline", "Ссылки и e-mail внутри текста"),
                          ("toc_lines", "Строки оглавления («Глава 1 ........ 12»)"),
                          ("no_letters", "Строки без букв (цифры, символы, линии)"),
                          ("footnote_marks", "Знаки сносок: [12], ¹², *"),
                          ("footnote_numbers", "Номера сносок, приклеенные к тексту: «…»1087, слово12 "
                                               "(только если идут по порядку)"),
                          ("footnote_lines", "Абзацы-сноски внутри текста: «18 Кол. 2:3» "
                                             "(только если номера идут по порядку)"),
                          ("square_brackets", "Всё в [квадратных скобках]"),
                          ("parentheses", "Всё в (круглых скобках)")):
            cb = QCheckBox(text)
            cb.toggled.connect(self._on_filter_ui_changed)
            self.chk_f[key] = cb
            f.addWidget(cb)
        row = QHBoxLayout()
        row.addWidget(QLabel("Пропускать строки короче"))
        self.sp_short = QSpinBox()
        self.sp_short.setRange(0, 200)
        self.sp_short.setSpecialValueText("выкл.")
        self.sp_short.setSuffix(" симв.")
        self.sp_short.valueChanged.connect(self._on_filter_ui_changed)
        row.addWidget(self.sp_short)
        row.addStretch(1)
        f.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Колонтитулы")
        f = QVBoxLayout(g)
        row = QHBoxLayout()
        row.addWidget(QLabel("Зона колонтитула: первые/последние"))
        self.sp_edge = QSpinBox()
        self.sp_edge.setRange(1, 10)
        self.sp_edge.setSuffix(" строк(и)")
        self.sp_edge.valueChanged.connect(self._on_filter_ui_changed)
        row.addWidget(self.sp_edge)
        row.addStretch(1)
        f.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Повторяется не менее чем на"))
        self.sp_ratio = QSpinBox()
        self.sp_ratio.setRange(5, 100)
        self.sp_ratio.setValue(30)
        self.sp_ratio.setSuffix(" % страниц")
        row.addWidget(self.sp_ratio)
        b = QPushButton("Найти колонтитулы")
        b.clicked.connect(self.detect_headers)
        row.addWidget(b)
        row.addStretch(1)
        f.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Свои правила")
        f = QVBoxLayout(g)
        self.rules_table = QTableWidget(0, 5)
        self.rules_table.setHorizontalHeaderLabels(["Текст / шаблон", "Тип", "Где", "Действие", "Аа"])
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            self.rules_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rules_table.setMinimumHeight(180)
        self.rules_table.itemChanged.connect(self._on_rule_item_changed)
        f.addWidget(self.rules_table)
        row = QHBoxLayout()
        b1 = QPushButton("Добавить")
        b1.clicked.connect(lambda: self._add_rule_dialog(textproc.new_rule()))
        b2 = QPushButton("Изменить")
        b2.clicked.connect(self._edit_rule)
        b3 = QPushButton("Удалить")
        b3.clicked.connect(self._del_rules)
        b4 = QPushButton("Сбросить ручные отметки")
        b4.clicked.connect(self._reset_overrides)
        for b in (b1, b2, b3, b4):
            row.addWidget(b)
        row.addStretch(1)
        f.addLayout(row)
        tip = QLabel("Типы: «Содержит» — подстрока; «Шаблон» — строка целиком, цифры в ней любые "
                     "(для колонтитулов с номерами); «Регулярное выражение» — Python regex. "
                     "Действие «Вырезать совпадение» удаляет только найденный фрагмент, а не всю строку.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        f.addWidget(tip)
        v.addWidget(g)
        self.lbl_filter_stats = QLabel("")
        v.addWidget(self.lbl_filter_stats)
        v.addStretch(1)
        return w

    # ---- панель «Текст»
    def _text_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Сборка абзацев")
        f = QVBoxLayout(g)
        self.chk_join = QCheckBox("Склеивать строки внутри абзаца (для PDF)")
        self.chk_hyph = QCheckBox("Убирать переносы слов «пере-\\nнос»")
        self.chk_merge = QCheckBox("Склеивать абзацы, разорванные границей страницы")
        for c in (self.chk_join, self.chk_hyph, self.chk_merge):
            c.toggled.connect(self._on_text_opts_changed)
            f.addWidget(c)
        row = QHBoxLayout()
        row.addWidget(QLabel("Делить абзацы длиннее"))
        self.sp_maxpara = QSpinBox()
        self.sp_maxpara.setRange(300, 20000)
        self.sp_maxpara.setSingleStep(500)
        self.sp_maxpara.setSuffix(" симв.")
        self.sp_maxpara.valueChanged.connect(self._on_text_opts_changed)
        row.addWidget(self.sp_maxpara)
        row.addStretch(1)
        f.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Нормализация перед чтением")
        f = QFormLayout(g)
        self.cb_numbers = QComboBox()
        self.cb_numbers.addItem("Авто (по движку)", "auto")
        self.cb_numbers.addItem("Всегда", "on")
        self.cb_numbers.addItem("Никогда", "off")
        f.addRow("Числа — словами:", self.cb_numbers)
        self.cb_translit = QComboBox()
        self.cb_translit.addItem("Авто (по движку)", "auto")
        self.cb_translit.addItem("Всегда", "on")
        self.cb_translit.addItem("Никогда", "off")
        f.addRow("Латиницу — русскими буквами:", self.cb_translit)
        self.chk_abbr = QCheckBox("Раскрывать сокращения (т.е., т.к., и т.д., г., км…)")
        self.chk_symbols = QCheckBox("Читать символы (№, §, %, °, ±…)")
        f.addRow(self.chk_abbr)
        f.addRow(self.chk_symbols)
        self.chk_smart_accent = QCheckBox("Ставить ударения и букву «ё» по смыслу (умные ударения)")
        self.chk_smart_accent.setToolTip("Работает с голосами Silero и Piper, если скачаны модели "
                                         "(окно «Голоса» → «Умные ударения»)")
        f.addRow(self.chk_smart_accent)
        self.lbl_smart_accent = QLabel()
        self.lbl_smart_accent.setStyleSheet("color: gray;")
        self.lbl_smart_accent.setWordWrap(True)
        self.lbl_smart_accent.setOpenExternalLinks(False)
        self.lbl_smart_accent.linkActivated.connect(lambda _: self.voices_dialog(tab="accent"))
        f.addRow(self.lbl_smart_accent)
        self.chk_smart_accent.toggled.connect(self._save_text_opts)
        for c in (self.cb_numbers, self.cb_translit):
            c.currentIndexChanged.connect(self._save_text_opts)
        for c in (self.chk_abbr, self.chk_symbols):
            c.toggled.connect(self._save_text_opts)
        v.addWidget(g)

        g = QGroupBox("Словарь замен (произношение)")
        f = QVBoxLayout(g)
        self.repl_table = QTableWidget(0, 4)
        self.repl_table.setHorizontalHeaderLabels(["Найти", "Читать как", "Regex", "Аа"])
        self.repl_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.repl_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.repl_table.verticalHeader().setVisible(False)
        self.repl_table.setMinimumHeight(160)
        self.repl_table.itemChanged.connect(self._on_repl_changed)
        f.addWidget(self.repl_table)
        row = QHBoxLayout()
        b1 = QPushButton("Добавить")
        b1.clicked.connect(self._add_repl)
        b2 = QPushButton("Удалить")
        b2.clicked.connect(self._del_repl)
        b3 = QPushButton("Показать, как будет прочитано")
        b3.clicked.connect(self._preview_normalized)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        row.addStretch(1)
        f.addLayout(row)
        tip = QLabel("Пример: «ГОСТ» → «гост», «Linux» → «линукс», «замок» → «з+амок» "
                     "(«+» перед гласной — ударение, понимают голоса Silero и Piper). "
                     "Замены применяются только к "
                     "озвучке, текст на экране не меняется.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        f.addWidget(tip)
        v.addWidget(g)
        v.addStretch(1)
        return w

    # ---- панель «Сохранение в файл»
    def _export_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        top = QLabel("Книгу можно записать в звуковой файл и слушать на телефоне, "
                     "в плеере или в машине — без этой программы.")
        top.setWordWrap(True)
        v.addWidget(top)
        b = QPushButton("💾  Сохранить эту книгу в аудиофайл…")
        b.setMinimumHeight(44)
        b.setToolTip("Программа спросит, куда сохранить, и начнёт запись. Пока идёт запись, "
                     "можно пользоваться программой дальше.")
        b.clicked.connect(self.export_dialog)
        v.addWidget(b)
        self.lbl_estimate = QLabel("")
        self.lbl_estimate.setWordWrap(True)
        v.addWidget(self.lbl_estimate)

        g = QGroupBox("Как сохранить")
        f = QFormLayout(g)
        self.cb_split = QComboBox()
        for k, t in SPLIT_MODES.items():
            self.cb_split.addItem(t, k)
        self.cb_split.setToolTip("Главы программа находит сама: по оглавлению книги или по строкам "
                                 "вида «Глава 5», «Часть вторая», «Пролог».\n"
                                 "Файлы называются «Книга 01 - Название главы».")
        f.addRow("Разбить на файлы:", self.cb_split)
        self.sp_split = QSpinBox()
        self.sp_split.setRange(1, 600)
        self.sp_split.setSingleStep(5)
        self.sp_split.setSuffix(" мин")
        f.addRow("Длина одного файла:", self.sp_split)
        self.lbl_chapters = QLabel("")
        self.lbl_chapters.setStyleSheet("color: gray;")
        self.lbl_chapters.setWordWrap(True)
        f.addRow(self.lbl_chapters)
        self.cb_range = QComboBox()
        self.cb_range.addItem("Всю книгу", "all")
        self.cb_range.addItem("С места, где стоит курсор в тексте", "from")
        self.cb_range.addItem("Только выделенный кусок текста", "sel")
        f.addRow("Что сохранить:", self.cb_range)
        v.addWidget(g)

        g = QGroupBox("Файл")
        f = QFormLayout(g)
        self.cb_format = QComboBox()
        for k, (_, _, title) in EXPORT_FORMATS.items():
            self.cb_format.addItem(title, k)
        self.cb_format.setToolTip("MP3 открывается везде — на телефоне, компьютере, в машине.")
        f.addRow("Формат:", self.cb_format)
        self.cb_quality = QComboBox()
        for k in QUALITY_LEVELS:
            self.cb_quality.addItem(k)
        f.addRow("Качество звука:", self.cb_quality)
        self.lbl_quality = QLabel("")
        self.lbl_quality.setStyleSheet("color: gray;")
        self.lbl_quality.setWordWrap(True)
        f.addRow(self.lbl_quality)
        self.ed_artist = QLineEdit()
        self.ed_artist.setPlaceholderText("например, Л. Н. Толстой")
        self.ed_artist.setToolTip("Будет видно в плеере на телефоне")
        f.addRow("Автор книги:", self.ed_artist)
        v.addWidget(g)

        g = QGroupBox("Несколько книг сразу")
        gv = QVBoxLayout(g)
        b2 = QPushButton("📚  Выбрать книги и сохранить все в аудио…")
        b2.setMinimumHeight(36)
        b2.setToolTip("Каждая книга будет записана в свой файл с текущим голосом и настройками.")
        b2.clicked.connect(self.add_files_to_queue)
        gv.addWidget(b2)
        b3 = QPushButton("Посмотреть, как идёт сохранение")
        b3.clicked.connect(lambda: self._show_queue())
        gv.addWidget(b3)
        v.addWidget(g)
        v.addStretch(1)
        for c in (self.cb_format, self.cb_quality):
            c.currentIndexChanged.connect(self._save_export)
        self.cb_split.currentIndexChanged.connect(self._save_export)
        self.sp_split.valueChanged.connect(self._save_export)
        self.ed_artist.textChanged.connect(self._save_export)
        return w

    # ----------------------------------------------------------- settings <-> UI
    def _load_settings_to_ui(self):
        s = self.settings
        self._updating_ui = True
        p = s["params"]
        self.sl_speed.setValue(p["speed"])
        self.sl_pitch.setValue(p["pitch"])
        self.sl_volume.setValue(p["volume"])
        self.sp_sent.setValue(int(p["sentence_pause_ms"]))
        self.sp_para.setValue(int(p["paragraph_pause_ms"]))
        self.chk_trim.setChecked(p["trim_silence"])
        self.sp_noise.setValue(p["piper_noise_scale"] or 0)
        self.sp_noisew.setValue(p["piper_noise_w"] or 0)
        self.cb_silero_sr.setCurrentIndex(max(0, self.cb_silero_sr.findData(p["silero_sample_rate"])))
        self.chk_accent.setChecked(p["silero_accent"])
        self.chk_yo.setChecked(p["silero_yo"])
        self.sp_threads.setValue(int(p.get("silero_threads") or 0))
        idx = self.cb_device.findData(s["output_device"])
        self.cb_device.setCurrentIndex(max(0, idx))
        self.ed_test.setText(s["test_phrase"])

        t = s["text"]
        self.chk_join.setChecked(t["join_lines"])
        self.chk_hyph.setChecked(t["fix_hyphen"])
        self.chk_merge.setChecked(t["merge_broken"])
        self.sp_maxpara.setValue(int(t["max_para_chars"]))
        self.cb_numbers.setCurrentIndex(max(0, self.cb_numbers.findData(t["numbers"])))
        self.cb_translit.setCurrentIndex(max(0, self.cb_translit.findData(t["translit"])))
        self.chk_abbr.setChecked(t["abbreviations"])
        self.chk_symbols.setChecked(t["symbols"])
        self.chk_smart_accent.setChecked(t.get("smart_accent", True))
        self._update_accent_label()
        self._fill_repl_table()

        self._filter_to_ui()
        self._fill_profiles()

        e = s["export"]
        self.cb_format.setCurrentIndex(max(0, self.cb_format.findData(e["format"])))
        self.cb_quality.setCurrentText(e["quality"])
        mode = e.get("split_mode") or ("minutes" if e.get("split_minutes") else "none")
        self.cb_split.setCurrentIndex(max(0, self.cb_split.findData(mode)))
        self.sp_split.setValue(int(e.get("split_minutes") or 30))
        self.sp_split.setEnabled("minutes" in mode)
        self.ed_artist.setText(e.get("artist", ""))

        self.chk_follow.setChecked(s["follow_reading"])
        self.chk_show_excl.setChecked(s["show_excluded"])
        self.lines_model.show_excluded = s["show_excluded"]
        self._fill_recent()

        ei = self.cb_engine.findData(s["engine"])
        self.cb_engine.setCurrentIndex(max(0, ei))
        self._updating_ui = False
        self._on_engine_changed()
        self._update_quality_label()

    def _filter_to_ui(self):
        cfg = self.settings["filter"]
        old = self._updating_ui
        self._updating_ui = True
        for k, cb in self.chk_f.items():
            cb.setChecked(bool(cfg.get(k, textproc.DEFAULT_FILTER[k])))
        self.sp_short.setValue(int(cfg.get("short_lines", 0) or 0))
        self.sp_edge.setValue(int(cfg.get("edge_lines", 3)))
        self._fill_rules_table()
        self._updating_ui = old

    def _fill_rules_table(self):
        old = self._updating_ui
        self._updating_ui = True
        rules = self.settings["filter"].get("rules", [])
        self.rules_table.setRowCount(0)
        for r in rules:
            row = self.rules_table.rowCount()
            self.rules_table.insertRow(row)
            it = QTableWidgetItem(r["pattern"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if r.get("enabled", True) else Qt.Unchecked)
            self.rules_table.setItem(row, 0, it)
            for c, (val, table) in enumerate(((r["kind"], textproc.RULE_KINDS),
                                              (r["where"], textproc.RULE_WHERE),
                                              (r["action"], textproc.RULE_ACTION)), start=1):
                it = QTableWidgetItem(table.get(val, val))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.rules_table.setItem(row, c, it)
            it = QTableWidgetItem("да" if r.get("case") else "")
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.rules_table.setItem(row, 4, it)
        self._updating_ui = old

    def _on_rule_item_changed(self, item):
        if self._updating_ui or item.column() != 0:
            return
        rules = self.settings["filter"]["rules"]
        r = item.row()
        if r < len(rules):
            rules[r]["pattern"] = item.text()
            rules[r]["enabled"] = item.checkState() == Qt.Checked
            self._filter_changed()

    def _add_rule_dialog(self, rule):
        d = RuleDialog(rule, self)
        if d.exec() == QDialog.Accepted and d.rule()["pattern"]:
            self.settings["filter"].setdefault("rules", []).append(d.rule())
            self._fill_rules_table()
            self._filter_changed()

    def _edit_rule(self):
        rows = sorted({i.row() for i in self.rules_table.selectedIndexes()})
        if not rows:
            return
        rules = self.settings["filter"]["rules"]
        d = RuleDialog(rules[rows[0]], self)
        if d.exec() == QDialog.Accepted:
            nr = d.rule()
            nr["enabled"] = rules[rows[0]].get("enabled", True)
            rules[rows[0]] = nr
            self._fill_rules_table()
            self._filter_changed()

    def _del_rules(self):
        rows = sorted({i.row() for i in self.rules_table.selectedIndexes()}, reverse=True)
        rules = self.settings["filter"]["rules"]
        for r in rows:
            if r < len(rules):
                rules.pop(r)
        self._fill_rules_table()
        self._filter_changed()

    def _on_filter_ui_changed(self, *_):
        if self._updating_ui:
            return
        cfg = self.settings["filter"]
        for k, cb in self.chk_f.items():
            cfg[k] = cb.isChecked()
        cfg["short_lines"] = self.sp_short.value()
        cfg["edge_lines"] = self.sp_edge.value()
        self._filter_changed()

    def _filter_changed(self):
        self.settings.save()
        if self.doc:
            self.status = textproc.evaluate_lines(self.doc, self.settings["filter"])
            self.lines_model.update_status(self.status)
            self.schedule_rebuild()

    def _reset_overrides(self):
        self.overrides.clear()
        if self.doc:
            self.lines_model.set_data(self.doc, self.status, self.overrides)
        self.schedule_rebuild()

    # ---- профили
    def _fill_profiles(self):
        self.cb_profile.clear()
        for name in sorted(self.settings["filter_profiles"]):
            self.cb_profile.addItem(name)

    def _profile_save(self):
        name, ok = QInputDialog.getText(self, "Профиль фильтра", "Название профиля:",
                                        text=self.cb_profile.currentText())
        if ok and name.strip():
            self.settings["filter_profiles"][name.strip()] = copy.deepcopy(self.settings["filter"])
            self.settings.save()
            self._fill_profiles()
            self.cb_profile.setCurrentText(name.strip())

    def _profile_load(self):
        name = self.cb_profile.currentText()
        prof = self.settings["filter_profiles"].get(name)
        if prof:
            cfg = copy.deepcopy(textproc.DEFAULT_FILTER)
            cfg.update(copy.deepcopy(prof))
            self.settings["filter"] = cfg
            self._filter_to_ui()
            self._filter_changed()

    def _profile_delete(self):
        name = self.cb_profile.currentText()
        if name and QMessageBox.question(self, "Профиль", f"Удалить профиль «{name}»?") == QMessageBox.Yes:
            self.settings["filter_profiles"].pop(name, None)
            self.settings.save()
            self._fill_profiles()

    # ---- текст
    def _on_text_opts_changed(self, *_):
        self._save_text_opts()
        self.schedule_rebuild()

    def _save_text_opts(self, *_):
        if self._updating_ui:
            return
        t = self.settings["text"]
        t["join_lines"] = self.chk_join.isChecked()
        t["fix_hyphen"] = self.chk_hyph.isChecked()
        t["merge_broken"] = self.chk_merge.isChecked()
        t["max_para_chars"] = self.sp_maxpara.value()
        t["numbers"] = self.cb_numbers.currentData()
        t["translit"] = self.cb_translit.currentData()
        t["abbreviations"] = self.chk_abbr.isChecked()
        t["symbols"] = self.chk_symbols.isChecked()
        t["smart_accent"] = self.chk_smart_accent.isChecked()
        self.settings.save()
        if t["smart_accent"]:
            accent.preload_async()

    def _fill_repl_table(self):
        old = self._updating_ui
        self._updating_ui = True
        self.repl_table.setRowCount(0)
        for r in self.settings["text"]["replacements"]:
            row = self.repl_table.rowCount()
            self.repl_table.insertRow(row)
            self.repl_table.setItem(row, 0, QTableWidgetItem(r.get("find", "")))
            self.repl_table.setItem(row, 1, QTableWidgetItem(r.get("replace", "")))
            for c, key in ((2, "regex"), (3, "case")):
                it = QTableWidgetItem("")
                it.setFlags((it.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
                it.setCheckState(Qt.Checked if r.get(key) else Qt.Unchecked)
                self.repl_table.setItem(row, c, it)
        self._updating_ui = old

    def _on_repl_changed(self, *_):
        if self._updating_ui:
            return
        reps = []
        for row in range(self.repl_table.rowCount()):
            g = lambda c: self.repl_table.item(row, c)
            if g(0) is None:
                continue
            reps.append({"find": g(0).text(), "replace": g(1).text() if g(1) else "",
                         "regex": g(2) is not None and g(2).checkState() == Qt.Checked,
                         "case": g(3) is not None and g(3).checkState() == Qt.Checked,
                         "enabled": True})
        self.settings["text"]["replacements"] = reps
        self.settings.save()

    def _add_repl(self):
        sel = self.editor.textCursor().selectedText().strip()
        self.settings["text"]["replacements"].append({"find": sel, "replace": sel, "regex": False,
                                                      "case": False, "enabled": True})
        self._fill_repl_table()
        self.settings.save()
        self.repl_table.editItem(self.repl_table.item(self.repl_table.rowCount() - 1, 0 if not sel else 1))

    def _del_repl(self):
        rows = sorted({i.row() for i in self.repl_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.repl_table.removeRow(r)
        self._on_repl_changed()

    def _preview_normalized(self):
        paras = self._current_play_texts()
        if not paras:
            QMessageBox.information(self, APP_NAME, "Нет текста.")
            return
        idx = self._para_at_cursor()
        sample = paras[idx:idx + 5]
        cls = ENGINES[self.cb_engine.currentData()]
        out = []
        for p in sample:
            norm = textproc.normalize_for_tts(p, self.settings["text"], cls.caps())
            if cls.stress_style != "none" and self.settings["text"].get("smart_accent", True):
                norm = accent.accent_text(norm)
            out.append(textproc.apply_stress_style(norm, "plus" if cls.stress_style != "none" else "none"))
        d = QDialog(self)
        d.setWindowTitle("Так текст уйдёт в синтезатор")
        d.resize(700, 450)
        v = QVBoxLayout(d)
        te = QPlainTextEdit("\n\n".join(out))
        te.setReadOnly(True)
        v.addWidget(QLabel(f"Движок: {cls.title}. Показаны абзацы {idx + 1}–{idx + len(sample)}."))
        v.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        d.exec()

    # ---- голос
    def _on_engine_changed(self, *_):
        if self._updating_ui:
            return
        eid = self.cb_engine.currentData()
        cls = ENGINES[eid]
        ok, msg = cls.available()
        self.lbl_engine.setText(cls.description + ("" if ok else f"<br><b style='color:#c55'>{msg}</b>"))
        self.settings["engine"] = eid
        self._on_engine_changed_visibility()
        self.sl_pitch.setEnabled(cls.supports_pitch)
        self._fill_voices()
        self.settings.save()
        self._update_estimate()

    def _fill_voices(self):
        eid = self.cb_engine.currentData()
        cls = ENGINES[eid]
        self._updating_ui = True
        self.cb_voice.clear()
        try:
            voices = cls.list_voices()
        except Exception:
            voices = []
        # русские — первыми
        voices.sort(key=lambda v: (not v.lang.lower().startswith("ru"), v.name.lower()))
        for vi in voices:
            self.cb_voice.addItem(f"{vi.name}", vi.id)
        if not voices:
            self.cb_voice.addItem("— нет голосов, нажмите «Скачать…» —", None)
        want = self.settings["voices"].get(eid)
        i = self.cb_voice.findData(want)
        self.cb_voice.setCurrentIndex(max(0, i))
        self._updating_ui = False
        self._on_voice_changed()

    def _on_voice_changed(self, *_):
        if self._updating_ui:
            return
        vid = self.cb_voice.currentData()
        if vid:
            self.settings["voices"][self.cb_engine.currentData()] = vid
            self.settings.save()
        self.calib_timer.start()
        self._update_estimate()

    def _save_params(self, *_):
        if self._updating_ui:
            return
        p = self.settings["params"]
        p["speed"] = round(self.sl_speed.value(), 3)
        p["pitch"] = round(self.sl_pitch.value(), 3)
        p["volume"] = round(self.sl_volume.value(), 3)
        p["sentence_pause_ms"] = self.sp_sent.value()
        p["paragraph_pause_ms"] = self.sp_para.value()
        p["trim_silence"] = self.chk_trim.isChecked()
        p["piper_noise_scale"] = self.sp_noise.value() or None
        p["piper_noise_w"] = self.sp_noisew.value() or None
        p["silero_sample_rate"] = self.cb_silero_sr.currentData()
        p["silero_accent"] = self.chk_accent.isChecked()
        p["silero_yo"] = self.chk_yo.isChecked()
        p["silero_threads"] = self.sp_threads.value()
        self.settings["output_device"] = self.cb_device.currentData()
        self.settings.save()
        self.calib_timer.start()     # скорость могла измениться — уточним замер
        self._update_estimate()

    def _save_export(self, *_):
        if self._updating_ui:
            return
        e = self.settings["export"]
        e["format"] = self.cb_format.currentData()
        e["quality"] = self.cb_quality.currentText()
        e["split_mode"] = self.cb_split.currentData()
        e["split_minutes"] = self.sp_split.value()
        e["artist"] = self.ed_artist.text()
        self.sp_split.setEnabled("minutes" in e["split_mode"])
        self._update_chapters_label()
        self.settings.save()
        self._update_quality_label()

    def _update_quality_label(self):
        fmt = self.cb_format.currentData()
        q = QUALITY_LEVELS.get(self.cb_quality.currentText(), 0.25)
        self.cb_quality.setEnabled(fmt in ("mp3", "ogg"))
        # примерный размер часа записи (голоса Piper, 22 кГц)
        if fmt == "mp3":
            mb = {0.0: 70, 0.25: 55, 0.5: 35, 0.75: 20}.get(q, 55)
        elif fmt == "ogg":
            mb = {0.0: 40, 0.25: 30, 0.5: 25, 0.75: 20}.get(q, 30)
        elif fmt == "flac":
            mb = 100
        else:
            mb = 160
        tip = " Для голоса «Среднего» качества достаточно." if fmt in ("mp3", "ogg") else ""
        self.lbl_quality.setText(f"Час звучания займёт около {mb} МБ.{tip}")

    def voices_dialog(self, tab: str | None = None):
        d = VoicesDialog(self.settings, self, tab=tab)
        d.exec()
        self.settings.save()
        self._fill_voices()
        self._on_engine_changed_visibility()
        self._update_accent_label()

    def _update_accent_label(self):
        if accent.installed():
            self.lbl_smart_accent.setText("Модели скачаны.")
        else:
            self.lbl_smart_accent.setText('Модели не скачаны — <a href="#">скачать (около '
                                          f'{accent.TOTAL_BYTES // 1_000_000} МБ)</a>.')

    # ------------------------------------------------------------- документ
    def _fill_recent(self):
        self.recent_menu.clear()
        rec = [p for p in self.settings["recent"] if os.path.exists(p)]
        for p in rec[:15]:
            a = self.recent_menu.addAction(Path(p).name)
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, p=p: self.open_file(p))
        if not rec:
            a = self.recent_menu.addAction("(пусто)")
            a.setEnabled(False)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть книгу", self.settings["last_dir"],
                                              FILE_FILTER)
        if path:
            self.open_file(path)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.open_file(urls[0].toLocalFile())

    def open_file(self, path: str):
        if self.load_thread and self.load_thread.isRunning():
            return
        self.stop()
        self.sb_doc.setText(f"Открываю «{Path(path).name}», подождите…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        log.info(f"Открываю книгу: {describe_file(path)}")
        self._open_t = time.time()
        th = FuncThread(lambda progress, stop, p: load_document(p), path, parent=self,
                        name="открытие книги")
        th.done.connect(lambda doc: self._on_doc_loaded(doc, path))
        th.failed.connect(self._on_doc_failed)
        self.load_thread = th
        th.start()

    def _on_doc_failed(self, err):
        QApplication.restoreOverrideCursor()
        self.sb_doc.setText("")
        self.show_error(f"Не получилось открыть эту книгу.\n\n{err}")

    def _on_doc_loaded(self, doc, path):
        QApplication.restoreOverrideCursor()
        log.info(f"Книга открыта за {time.time() - getattr(self, '_open_t', time.time()):.1f} с: "
                 f"{doc.page_label.lower()} {doc.page_count}, строк {len(doc.lines)}, "
                 f"глав {len(doc.chapters)}" + (f", предупреждения: {'; '.join(doc.warnings)}" if doc.warnings else ""))
        self.doc = doc
        self.overrides = {}
        self.settings["last_dir"] = str(Path(path).parent)
        rec = [p for p in self.settings["recent"] if p != path]
        self.settings["recent"] = [path] + rec[:19]
        self.settings.save()
        self._fill_recent()
        self.status = textproc.evaluate_lines(doc, self.settings["filter"])
        self.lines_model.set_data(doc, self.status, self.overrides)
        self.chk_manual.setChecked(False)
        self.manual_text = False
        self.setWindowTitle(f"{Path(path).name} — {APP_NAME}")
        self.rebuild()
        for w in doc.warnings:
            QMessageBox.information(self, APP_NAME, w)
        # колонтитулы — предложим найти автоматически
        if doc.lines:
            cands = textproc.detect_headers(doc, self.sp_edge.value(), self.sp_ratio.value() / 100)
            existing = {textproc.template_key(r["pattern"]) for r in self.settings["filter"]["rules"]
                        if r.get("kind") == "template"}
            cands = [c for c in cands if c.key not in existing and
                     any(self.lines_model.included(i) for i, ln in enumerate(doc.lines)
                         if textproc.template_key(ln.text) == c.key)]
            if cands:
                QTimer.singleShot(0, lambda: self._offer_headers(cands))

    def _offer_headers(self, cands):
        ex = "\n".join(f"  • {c.example[:70]}  ({c.pages} раз)" for c in cands[:5])
        if QMessageBox.question(self, APP_NAME,
                                f"В книге есть строки, которые повторяются на многих страницах "
                                f"(похоже на колонтитулы):\n\n{ex}\n\nВыбрать, какие из них не читать?") \
                == QMessageBox.Yes:
            d = HeaderDialog(cands, self.doc.page_count, self.doc.page_label, self)
            if d.exec() == QDialog.Accepted:
                self._add_header_rules(d.chosen())

    def detect_headers(self):
        if not self.doc:
            QMessageBox.information(self, APP_NAME, "Сначала откройте книгу.")
            return
        cands = textproc.detect_headers(self.doc, self.sp_edge.value(), self.sp_ratio.value() / 100)
        if not cands:
            QMessageBox.information(self, APP_NAME, "Повторяющихся строк, похожих на колонтитулы, "
                                                    "не найдено. Попробуйте уменьшить процент "
                                                    "или увеличить зону.")
            return
        d = HeaderDialog(cands, self.doc.page_count, self.doc.page_label, self)
        if d.exec() == QDialog.Accepted:
            self._add_header_rules(d.chosen())

    def _add_header_rules(self, cands):
        rules = self.settings["filter"].setdefault("rules", [])
        have = {(r["kind"], textproc.template_key(r["pattern"])) for r in rules}
        for c in cands:
            if ("template", textproc.template_key(c.example)) not in have:
                # найденные без страниц ищутся по всему тексту, а не у края «страницы»
                rules.append(textproc.new_rule(c.example, "template", c.where))
        self._fill_rules_table()
        self._filter_changed()

    def schedule_rebuild(self):
        self.rebuild_timer.start()

    def _force_rebuild(self):
        if self.manual_text and QMessageBox.question(
                self, APP_NAME, "Ручные правки текста будут потеряны. Продолжить?") != QMessageBox.Yes:
            return
        self.chk_manual.setChecked(False)
        self.manual_text = False
        self.rebuild()

    def rebuild(self):
        if not self.doc:
            return
        included = [self.lines_model.included(i) for i in range(len(self.doc.lines))]
        self.paragraphs = textproc.build_paragraphs(self.doc, included, self.settings["filter"],
                                                    self.settings["text"])
        n_ex = included.count(False)
        self.lbl_filter_stats.setText(f"Исключено строк: {n_ex} из {len(included)}"
                                      + (f" (вручную: {len(self.overrides)})" if self.overrides else ""))
        if self.manual_text:
            self._update_estimate()
            return
        if self.player.state != "stopped":
            self.stop()
        text = "\n\n".join(p.text for p in self.paragraphs)
        self._setting_text = True
        self.editor.setPlainText(text)
        self._setting_text = False
        self._parse_editor()
        self._update_estimate()
        self._update_chapters_label()

    def _on_editor_changed(self):
        if getattr(self, "_setting_text", False):
            return
        if self.manual_text:
            if self.player.state != "stopped":
                self.stop()
            self._parse_editor()
            self._update_estimate()

    def _on_manual_toggled(self, on):
        self.manual_text = on
        self.editor.setReadOnly(not on)
        if on:
            self.left_tabs.setCurrentWidget(self.text_tab)

    def _parse_editor(self):
        """Разбивка текста редактора на абзацы (по пустым строкам)."""
        text = self.editor.toPlainText()
        self.play_paras = []
        for m in re.finditer(r"\S(?:.|\n(?!\s*\n))*", text):
            self.play_paras.append((m.start(), m.end(), m.group(0).replace("\n", " ")))

    def _current_play_texts(self) -> list[str]:
        return [p[2] for p in self.play_paras]

    def _para_at_cursor(self, pos: int | None = None) -> int:
        if pos is None:
            pos = self.editor.textCursor().position()
        for i, (s, e, _) in enumerate(self.play_paras):
            if pos <= e:
                return i
        return max(0, len(self.play_paras) - 1)

    def _update_estimate(self):
        self.estimate_timer.start()

    def _do_update_estimate(self):
        """Длительность звучания: по тексту «как произносится» и замеренной скорости голоса."""
        texts = self._current_play_texts()
        eid, vid = self.cb_engine.currentData(), self.cb_voice.currentData()
        params = self.settings["params"]
        rate, exact = engines.rate_for(self.settings.data.get("voice_rates") or {}, eid, vid or "",
                                       float(params.get("speed", 1.0)))
        try:
            sec = engines.estimate_seconds(texts, eid, rate, params, self.settings["text"])
        except Exception:
            sec = 0
        dur = fmt_duration(sec)
        note = "" if exact or not vid else " (уточняется…)"
        if self.doc:
            self.sb_doc.setText(f"{Path(self.doc.path).name}   •   {self.doc.page_label.lower()}: "
                                f"{self.doc.page_count}   •   звучание ≈ {dur}{note}")
        self.lbl_estimate.setText(f"Запись займёт примерно {dur} звучания{note}." if texts else "")

    def _calibrate_voice(self):
        """Один раз для каждого голоса и скорости замеряем, как быстро он говорит
        (1–2 секунды в фоне) — по этому замеру считается длительность."""
        eid, vid = self.cb_engine.currentData(), self.cb_voice.currentData()
        if not vid or not ENGINES[eid].available()[0]:
            return
        params = copy.deepcopy(self.settings["params"])
        key = engines.rate_key(eid, vid, float(params.get("speed", 1.0)))
        if key in (self.settings.data.get("voice_rates") or {}):
            return
        # тот же голос не нагружаем параллельно с чтением или записью — попробуем позже
        if (self.calib_thread and self.calib_thread.isRunning()) or self.player.state != "stopped" \
                or (self.queue.run_thread and self.queue.run_thread.isRunning()):
            self.calib_timer.start(5000)
            return
        th = FuncThread(lambda progress, stop: engines.measure_voice_rate(
            eid, vid, params, self.settings["text"]), parent=self)
        th.done.connect(lambda r: self._calibrated(key, r))
        th.failed.connect(lambda err: None)
        self.calib_thread = th
        th.start()

    def _calibrated(self, key: str, rate: float):
        self.settings.data.setdefault("voice_rates", {})[key] = round(float(rate), 3)
        self.settings.save()
        self._update_estimate()
        self.calib_timer.start(1500)      # пока замеряли, могли выбрать другой голос

    # ---- строки: меню, поиск
    def _selected_lines(self) -> list[int]:
        res = []
        for idx in self.lines_view.selectionModel().selectedIndexes():
            li = self.lines_model.line_index(idx.row())
            if li is not None:
                res.append(li)
        return sorted(res)

    def _lines_menu(self, pos):
        idx = self.lines_view.indexAt(pos)
        li = self.lines_model.line_index(idx.row()) if idx.isValid() else None
        if li is None:
            return
        sel = self._selected_lines() or [li]
        text = self.doc.lines[li].text
        m = QMenu(self)
        a1 = m.addAction(f"Не читать выделенные строки ({len(sel)})")
        a2 = m.addAction(f"Читать выделенные строки ({len(sel)})")
        a3 = m.addAction("Сбросить ручную отметку")
        m.addSeparator()
        short = text if len(text) < 50 else text[:47] + "…"
        a4 = m.addAction(f"Не читать все такие строки (шаблон «{short}»)")
        a5 = m.addAction("Не читать все строки, содержащие…")
        a6 = m.addAction("Создать правило из строки…")
        m.addSeparator()
        a7 = m.addAction("▶ Читать с этой строки")
        a8 = m.addAction("Перейти к тексту")
        act = m.exec(self.lines_view.viewport().mapToGlobal(pos))
        if act == a1:
            self.lines_model.set_override(sel, False)
        elif act == a2:
            self.lines_model.set_override(sel, True)
        elif act == a3:
            self.lines_model.set_override(sel, None)
        elif act == a4:
            self.settings["filter"]["rules"].append(textproc.new_rule(text, "template", "any"))
            self._fill_rules_table()
            self._filter_changed()
        elif act == a5:
            s, ok = QInputDialog.getText(self, "Правило", "Не читать строки, содержащие:", text=text)
            if ok and s:
                self.settings["filter"]["rules"].append(textproc.new_rule(s, "contains", "any"))
                self._fill_rules_table()
                self._filter_changed()
        elif act == a6:
            self._add_rule_dialog(textproc.new_rule(text, "contains"))
        elif act == a7:
            self._goto_line_text(li)
            self.play(start=self._para_at_cursor())
        elif act == a8:
            self._goto_line_text(li)

    def _line_double_clicked(self, idx):
        li = self.lines_model.line_index(idx.row())
        if li is not None:
            self._goto_line_text(li)

    def _goto_line_text(self, li: int):
        if self.manual_text or not self.paragraphs:
            self.left_tabs.setCurrentWidget(self.text_tab)
            return
        pi = 0
        for i, p in enumerate(self.paragraphs):
            if p.first_line <= li:
                pi = i
            else:
                break
        pi = min(pi, len(self.play_paras) - 1)
        if pi >= 0:
            c = self.editor.textCursor()
            c.setPosition(self.play_paras[pi][0])
            self.editor.setTextCursor(c)
            self.editor.centerCursor()
        self.left_tabs.setCurrentWidget(self.text_tab)

    def _search_rows(self) -> list[int]:
        m = self.lines_model
        return [r for r in range(m.rowCount()) if m.matches(r)]

    def _apply_search(self):
        """Поиск по мере ввода: подсвечиваем все совпадения и встаём на ближайшее."""
        self.lines_model.set_search(self.search.text())
        if not self.search.text().strip():
            self.lbl_found.setText("")
            return
        cur = self.lines_view.currentIndex().row()
        self.find_next(1, from_row=max(-1, cur - 1))

    def find_next(self, direction: int = 1, from_row: int | None = None):
        if not self.doc:
            return
        if search_key(self.search.text()) != self.lines_model.search_q:
            self.lines_model.set_search(self.search.text())
        rows = self._search_rows()
        if not rows:
            self.lbl_found.setText("не найдено" if self.search.text().strip() else "")
            return
        cur = self.lines_view.currentIndex().row() if from_row is None else from_row
        if direction > 0:
            r = next((x for x in rows if x > cur), rows[0])
        else:
            r = next((x for x in reversed(rows) if x < cur), rows[-1])
        idx = self.lines_model.index(r)
        self.lines_view.setCurrentIndex(idx)
        self.lines_view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        self.lbl_found.setText(f"{rows.index(r) + 1} из {len(rows)}")

    def _on_show_excluded(self, v):
        self.settings["show_excluded"] = v
        self.lines_model.set_show_excluded(v)

    # ---------------------------------------------------------- воспроизведение
    def _engine_voice(self):
        eid = self.cb_engine.currentData()
        vid = self.cb_voice.currentData()
        ok, msg = ENGINES[eid].available()
        if not ok:
            log.warning(f"Набор голосов {eid} недоступен: {msg}")
            self.show_error(msg)
            return None
        if not vid:
            if QMessageBox.question(self, APP_NAME, "Не выбран голос для чтения.\n\nОткрыть окно, где можно скачать голос?") == QMessageBox.Yes:
                self.voices_dialog()
            return None
        return eid, vid

    def play_pause(self):
        st = self.player.state
        if st == "playing":
            self.player.pause()
        elif st == "paused":
            self.player.resume()
        elif st == "loading":
            pass
        else:
            self.play(start=self._para_at_cursor())
        self._update_actions()

    def play(self, start: int = 0, texts: list[str] | None = None):
        ev = self._engine_voice()
        if not ev:
            return
        if texts is None:
            texts = self._current_play_texts()
            self._playing_editor = True
        else:
            self._playing_editor = False
        if not texts:
            return
        self.player.play(texts, start, ev[0], ev[1], self.settings["params"], self.settings["text"],
                         device=self.settings["output_device"])
        self._last_hl = -2
        self._update_actions()

    def test_voice(self):
        self.settings["test_phrase"] = self.ed_test.text()
        self.settings.save()
        if self.player.state != "stopped":
            self.stop()
        self.play(0, [self.ed_test.text() or "Проверка голоса."])

    def stop(self):
        self.player.stop()
        self._clear_highlight()
        self._update_actions()

    def jump(self, delta: int):
        if not self.play_paras:
            return
        cur = self.player.current if self.player.state != "stopped" and getattr(self, "_playing_editor", True) \
            else self._para_at_cursor()
        new = max(0, min(len(self.play_paras) - 1, cur + delta))
        c = self.editor.textCursor()
        c.setPosition(self.play_paras[new][0])
        self.editor.setTextCursor(c)
        if self.player.state != "stopped":
            self.play(start=new)
        else:
            self._highlight(new)

    def _poll_player(self):
        p = self.player
        if p.error:
            err = p.error
            p.error = None
            self._clear_highlight()
            self.show_error(f"Не получилось прочитать текст вслух.\n\n{err}")
        if p.state in ("playing", "paused", "loading") and getattr(self, "_playing_editor", False):
            if p.current != getattr(self, "_last_hl", -2):
                self._last_hl = p.current
                self._highlight(p.current)
        if p.finished and getattr(self, "_was_active", False):
            self._clear_highlight()
        active = p.state != "stopped"
        if active != getattr(self, "_was_active", False):
            self._was_active = active
            self._update_actions()
        st = {"loading": "⏳ Подготовка голоса…", "playing": "▶ Чтение",
              "paused": "⏸ Пауза", "stopped": ""}[p.state]
        if p.state in ("playing", "paused") and getattr(self, "_playing_editor", False) and self.play_paras:
            st += f"  —  абзац {p.current + 1} из {len(self.play_paras)}"
        if self.now_label.text() != st:
            self.now_label.setText(st)
            self._update_actions()

    def _highlight(self, i: int):
        if not (0 <= i < len(self.play_paras)):
            return
        s, e, _ = self.play_paras[i]
        sel = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(255, 220, 120, 110))
        sel.format = fmt
        c = QTextCursor(self.editor.document())
        c.setPosition(s)
        c.setPosition(e, QTextCursor.KeepAnchor)
        sel.cursor = c
        self.editor.setExtraSelections([sel])
        if self.chk_follow.isChecked():
            # прокрутка без перемещения курсора пользователя, если он редактирует
            if not self.manual_text or not self.editor.hasFocus():
                cur = QTextCursor(self.editor.document())
                cur.setPosition(s)
                self.editor.setTextCursor(cur)
                self.editor.centerCursor()

    def _clear_highlight(self):
        self.editor.setExtraSelections([])

    def _update_actions(self):
        st = self.player.state
        style = self.style()
        if st == "playing":
            self.act_play.setIcon(style.standardIcon(QStyle.SP_MediaPause))
            self.act_play.setText("Пауза")
        else:
            self.act_play.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
            self.act_play.setText("Продолжить" if st == "paused" else "Читать")
        self.act_stop.setEnabled(st != "stopped")

    # ------------------------------------------------------------------ экспорт
    def _current_chapters(self) -> list[tuple[int, str]]:
        """Главы для текста в редакторе: из структуры документа, а если текст
        правился вручную — по абзацам-заголовкам «Глава N»."""
        if not self.manual_text and self.doc and len(self.paragraphs) == len(self.play_paras):
            return textproc.chapter_starts(self.doc, self.paragraphs)
        return textproc.chapter_starts_from_texts(self._current_play_texts())

    def _update_chapters_label(self):
        if not hasattr(self, "lbl_chapters"):
            return
        mode = self.cb_split.currentData()
        if not self.doc or "chapters" not in mode:
            self.lbl_chapters.setText("")
            return
        ch = self._current_chapters()
        if ch:
            names = ", ".join(t for _, t in ch[:3]) + (" …" if len(ch) > 3 else "")
            self.lbl_chapters.setText(f"Найдено глав: {len(ch)} — {names}")
        else:
            self.lbl_chapters.setText("Главы в этой книге не найдены — она будет "
                                      + ("разбита на части одинаковой длины." if "minutes" in mode
                                         else "сохранена одним файлом."))

    def _job_template(self, out_path: str, texts=None, source=None) -> ExportJob | None:
        ev = self._engine_voice()
        if not ev:
            return None
        fmt = self.cb_format.currentData()
        return ExportJob(
            out_path=out_path, fmt=fmt,
            quality=QUALITY_LEVELS.get(self.cb_quality.currentText(), 0.25),
            engine=ev[0], voice=ev[1], voice_name=self.cb_voice.currentText(),
            params=copy.deepcopy(self.settings["params"]),
            text_opts=copy.deepcopy(self.settings["text"]),
            split=self.sp_split.value(), title="", artist=self.ed_artist.text(),
            texts=texts, source=source, split_mode=self.cb_split.currentData(),
            filter_cfg=copy.deepcopy(self.settings["filter"]) if source else None)

    def _enough_disk(self, folder: Path, texts: list[str]) -> bool:
        """Запись толстой книги в WAV/FLAC легко занимает гигабайты — предупреждаем заранее."""
        import shutil
        try:
            free_mb = shutil.disk_usage(folder).free / 1e6
            eid, vid = self.cb_engine.currentData(), self.cb_voice.currentData() or ""
            params = self.settings["params"]
            rate, _ = engines.rate_for(self.settings.data.get("voice_rates") or {}, eid, vid,
                                       float(params.get("speed", 1.0)))
            hours = engines.estimate_seconds(texts, eid, rate, params, self.settings["text"]) / 3600
        except Exception:
            return True
        fmt = self.cb_format.currentData()
        q = QUALITY_LEVELS.get(self.cb_quality.currentText(), 0.25)
        per_hour = {"mp3": {0.0: 70, 0.25: 55, 0.5: 35, 0.75: 20}.get(q, 55),
                    "ogg": 40, "flac": 100, "wav": 160}.get(fmt, 60)
        if eid == "silero" and int(params.get("silero_sample_rate", 48000)) > 24000:
            per_hour *= 2
        need = hours * per_hour
        if need * 1.2 + 200 <= free_mb:
            return True
        log.warning(f"Мало места для записи: нужно ~{need:.0f} МБ, свободно {free_mb:.0f} МБ")
        return QMessageBox.question(
            self, APP_NAME,
            f"На диске может не хватить места.\n\nДля записи нужно примерно {need:.0f} МБ, "
            f"а свободно {free_mb:.0f} МБ.\n\nВсё равно начать запись?") == QMessageBox.Yes

    def _unique_path(self, path: str) -> str:
        """Не даём двум заданиям писать в один файл и не затираем молча существующий."""
        taken = {j.out_path.lower() for j in self.queue.jobs
                 if j.status in ("ожидает", "подготовка", "идёт")}
        p = Path(path)
        k = 2
        while str(p).lower() in taken:
            p = Path(path).with_name(f"{Path(path).stem} ({k}){Path(path).suffix}")
            k += 1
        return str(p)

    def export_dialog(self):
        """Добавить текущий документ (или его часть) в очередь экспорта."""
        texts = self._current_play_texts()
        chapters = self._current_chapters()
        mode = self.cb_range.currentData()
        if mode == "from":
            first = self._para_at_cursor()
            texts = texts[first:]
            chapters = textproc.shift_chapters(chapters, first)
        elif mode == "sel":
            chapters = []
            sel = self.editor.textCursor().selectedText().replace("\u2029", "\n")
            texts = [t.replace("\n", " ") for t in re.split(r"\n\s*\n", sel) if t.strip()]
        if not texts:
            QMessageBox.information(self, APP_NAME, "Нечего сохранять: сначала откройте книгу.")
            return
        if not self._engine_voice():
            return
        fmt = self.cb_format.currentData()
        base = Path(self.doc.path).stem if self.doc else "озвучка"
        start_dir = self.settings.data.get("export_dir") or self.settings["last_dir"] or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить аудио", str(Path(start_dir) / f"{base}.{fmt}"),
                                              f"Аудиофайл {EXPORT_FORMATS[fmt][0]} (*.{fmt})")
        if not path:
            return
        if not path.lower().endswith("." + fmt):
            path += "." + fmt
        if not self._enough_disk(Path(path).parent, texts):
            return
        self.settings["export_dir"] = str(Path(path).parent)
        job = self._job_template(self._unique_path(path), texts=list(texts))
        if not job:
            return
        job.chapters = chapters
        job.title = (self.doc.title if self.doc else "") or Path(path).stem
        self.queue.add(job)
        self._show_queue()

    def add_files_to_queue(self):
        """Пакетная озвучка: несколько документов с текущими фильтром и голосом."""
        if not self._engine_voice():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите книги (можно несколько, удерживая Ctrl)", self.settings["last_dir"],
                                                FILE_FILTER)
        if not paths:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Куда сохранить аудиофайлы? (Отмена — рядом с книгами)",
            self.settings.data.get("export_dir") or str(Path(paths[0]).parent))
        fmt = self.cb_format.currentData()
        for src in paths:
            folder = Path(out_dir) if out_dir else Path(src).parent
            job = self._job_template(self._unique_path(str(folder / f"{Path(src).stem}.{fmt}")), source=src)
            if job:
                self.queue.add(job)
        if out_dir:
            self.settings["export_dir"] = out_dir
        self.settings.save()
        self._show_queue()

    def _show_queue(self):
        self.left_tabs.setCurrentWidget(self.queue_widget)

    def _on_queue_changed(self):
        n = sum(1 for j in self.queue.jobs if j.status in ("ожидает", "подготовка", "идёт"))
        i = self.left_tabs.indexOf(self.queue_widget)
        self.left_tabs.setTabText(i, f"Сохранение ({n})" if n else "Сохранение")

    def _update_queue_status(self):
        self.sb_queue.setText(self.queue_widget.status_text())

    def _on_job_finished(self, job):
        self.settings["synth_rates"] = {f"{e}|{v}": r for (e, v), r in self.queue.rates.items()}
        self.settings.save()
        if job.status == "ошибка":
            self.statusBar().showMessage(f"Ошибка экспорта «{job.name()}»: {job.error}", 10000)
        elif job.status == "готово":
            self.statusBar().showMessage(f"Готово: {job.name()}", 6000)

    def _on_queue_all_finished(self):
        done = [j for j in self.queue.jobs if j.status == "готово"]
        failed = [j for j in self.queue.jobs if j.status == "ошибка"]
        if not done and not failed:
            return
        QApplication.alert(self)
        msg = f"Запись в аудио закончена. Готово книг: {len(done)}"
        if failed:
            msg += f", не получилось: {len(failed)}\n\n" + "\n".join(f"• {j.name()}: {j.error}" for j in failed[:5])
        self.statusBar().showMessage(msg.split("\n")[0], 15000)
        if failed:
            self.show_error(msg)

    # ------------------------------------------------------------------ закрытие
    def eventFilter(self, obj, e):
        if obj is self.editor and e.type() == QEvent.KeyPress and e.key() == Qt.Key_Space \
                and self.editor.isReadOnly() and not e.modifiers():
            self.play_pause()
            return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F3 and self.settings.data.get("advanced"):
            self.left_tabs.setCurrentWidget(self.lines_tab)
            self.find_next(-1 if e.modifiers() & Qt.ShiftModifier else 1)
            return
        if e.key() == Qt.Key_Space and not self.editor.hasFocus() and not self.search.hasFocus():
            self.play_pause()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ помощь
    def show_error(self, text: str):
        """Сообщение об ошибке с кнопкой, которая сразу сохраняет отчёт для поддержки."""
        log.warning(f"Показана ошибка: {text}")
        box = QMessageBox(QMessageBox.Warning, APP_NAME, text, QMessageBox.Ok, self)
        rep = box.addButton("Сообщить разработчику…", QMessageBox.ActionRole)
        box.setInformativeText("Если ошибка повторяется — сообщите разработчику: журнал с "
                               "подробностями приложится сам.")
        box.exec()
        if box.clickedButton() is rep:
            self.contact_developer(text)

    def contact_developer(self, problem: str = ""):
        from .contact_dialog import ContactDialog
        ContactDialog(self, problem if isinstance(problem, str) else "").exec()

    def _on_unhandled(self, exc):
        # сбой в коде окна: подробности уже в журнале; сообщаем не чаще раза в минуту
        now = time.time()
        if now - getattr(self, "_last_unhandled", 0) < 60:
            return
        self._last_unhandled = now
        QTimer.singleShot(0, lambda: self.show_error(
            f"В программе произошла ошибка: {type(exc).__name__}: {exc}\n\n"
            "Можно продолжать работу. Подробности записаны в журнал."))

    def save_report(self):
        from PySide6.QtCore import QStandardPaths
        desk = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        try:
            path = applog.make_report(Path(desk) if desk else None)
        except Exception as e:
            log.exception("Не удалось сохранить отчёт")
            QMessageBox.warning(self, APP_NAME, f"Не удалось сохранить отчёт: {e}")
            return
        QMessageBox.information(
            self, APP_NAME,
            f"Отчёт сохранён:\n{path}\n\nОтправьте этот файл тому, кто помогает вам с программой "
            "(например, в Telegram или по почте).\n\nВ отчёте: журнал работы программы, сведения о "
            "компьютере и настройки. Названий ваших книг и путей к ним там нет.")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def run_selftest(self):
        from ..selftest import format_results, run_checks, write_report
        if self.player.state != "stopped":
            self.stop()
        dlg = QDialog(self)
        dlg.setWindowTitle("Проверка программы")
        dlg.resize(760, 480)
        v = QVBoxLayout(dlg)
        lbl = QLabel("Проверяю… Это займёт до минуты: каждый голос произносит короткую фразу.")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        te = QPlainTextEdit()
        te.setReadOnly(True)
        v.addWidget(te, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        rep = bb.addButton("Сообщить разработчику…", QDialogButtonBox.ActionRole)
        rep.clicked.connect(lambda: self.contact_developer(
            "Результат проверки программы:\n" + te.toPlainText()))
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        th = FuncThread(lambda progress, stop: run_checks(lambda n: progress(n), stop, self.settings),
                        parent=self, name="самопроверка")
        th.progress.connect(lambda a: lbl.setText(f"Проверяю: {a[0]}…"))

        def done(res):
            write_report(res)
            bad = [c for c in res if not c.ok and not c.optional]
            lbl.setText("Всё в порядке." if not bad else
                        f"Найдены проблемы: {len(bad)}. Что сделать — написано под строками с ✘. "
                        "Если не помогло — сохраните отчёт о проблеме.")
            te.setPlainText(format_results(res) + "\n\n✔ — работает,  ✘ — проблема,  • — не настроено "
                            "(можно не обращать внимания)")

        th.done.connect(done)
        th.failed.connect(lambda err: lbl.setText(f"Проверка прервалась: {err}"))
        dlg.finished.connect(th.cancel)
        th.start()
        self._selftest_thread = th
        dlg.exec()

    def _open_doc_file(self, name: str):
        from ..paths import app_dir
        bases = (app_dir(), app_dir().parent, Path(__file__).resolve().parents[2])
        for p in [b / name for b in bases] + [b / "docs" / name for b in bases]:
            if p.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
                return
        QMessageBox.information(self, APP_NAME, f"Файл «{name}» не найден рядом с программой.")

    def _about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<b>DocVoice {__version__}</b><br>Озвучка книг и документов без интернета.<br><br>"
                          "Голоса: Piper (MIT), Silero (CC BY-NC), голоса Windows.<br>"
                          "Умные ударения: RUAccent, Denis Petrov (MIT).")

    def closeEvent(self, e):
        if self.queue.busy():
            if QMessageBox.question(self, APP_NAME, "Ещё не закончена запись книг в аудио.\n\n"
                                                    "Прервать запись и закрыть программу?") != QMessageBox.Yes:
                e.ignore()
                return
            self.queue.cancel_all()
            self.queue.wait(5000)
        self.player.stop()
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.settings["splitter"] = bytes(self.splitter.saveState().toBase64()).decode()
        self.settings["follow_reading"] = self.chk_follow.isChecked()
        self.settings.save()
        super().closeEvent(e)
