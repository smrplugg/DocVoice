"""Вкладка «Помощник»: команды словами → предложения, которые человек проверяет и применяет.
Появляется, только когда в настройках нейросети введён ключ."""
from __future__ import annotations

import copy
import html
import importlib.util
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout,
                               QWidget)

from .. import ai
from ..extract import Chapter
from ..log import log
from .common import FuncThread

EXAMPLES = ("Например: «убери сноски в квадратных скобках», «Гёте читай с ударением на первый слог», "
            "«найди главы и назови файлы по ним», «не читай предисловие», «исправь слипшиеся слова "
            "на 12-й странице», «сделай голос помедленнее и паузы между абзацами подлиннее».\n"
            "Помощник сам ищет по книге и помнит разговор — можно уточнять. "
            "Если выделить строки на вкладке «Что читать», он посмотрит именно на них. "
            "Ctrl+Enter — отправить.")


class AISettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.cfg = ai.config(settings)
        self.thread: FuncThread | None = None
        self.setWindowTitle("Нейросеть (помощник)")
        self.resize(680, 480)
        v = QVBoxLayout(self)
        note = QLabel("Помощник отправляет на сервер нейросети вашу просьбу, прошлые реплики разговора "
                      "и части текста книги (выборку и то, что он сам найдёт поиском; при проверке "
                      "произношения — всю книгу). Каждая просьба — обычно 1–4 запроса. "
                      "Ключ хранится только на этом компьютере "
                      "и в отчёты разработчику не попадает. Без ключа вкладки «Помощник» нет.")
        note.setWordWrap(True)
        v.addWidget(note)
        f = QFormLayout()
        self.url = QLineEdit(self.cfg.get("url") or ai.DEFAULT_URL)
        f.addRow("Адрес сервера:", self.url)
        krow = QHBoxLayout()
        self.key = QLineEdit(self.cfg.get("key", ""))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("sk-…")
        krow.addWidget(self.key, 1)
        show = QCheckBox("показать")
        show.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        krow.addWidget(show)
        f.addRow("Ключ:", krow)
        mrow = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(self.cfg.get("model") or ai.DEFAULT_MODEL)
        mrow.addWidget(self.model, 1)
        self.btn_list = QPushButton("Список моделей")
        self.btn_list.setToolTip("Загрузить с сервера модели, доступные ключу (бесплатные — сверху)")
        self.btn_list.clicked.connect(self._load_models)
        mrow.addWidget(self.btn_list)
        f.addRow("Модель:", mrow)
        v.addLayout(f)

        fb = {x.get("name"): x for x in self.cfg.get("fallbacks", [])}
        self.gem = fb.get("Gemini") or dict(ai.DEFAULT_FALLBACKS[0])
        self.pol = fb.get("Pollinations") or dict(ai.DEFAULT_FALLBACKS[1])
        v.addWidget(QLabel("<b>Запасные</b> — если основная не ответила (лимит, сбой, нет денег на счёте):"))
        g = QFormLayout()
        grow = QHBoxLayout()
        self.gem_on = QCheckBox()
        self.gem_on.setChecked(bool(self.gem.get("enabled", True)))
        grow.addWidget(self.gem_on)
        self.gem_key = QLineEdit(self.gem.get("key", ""))
        self.gem_key.setEchoMode(QLineEdit.Password)
        self.gem_key.setPlaceholderText("бесплатный ключ из aistudio.google.com (AIza…)")
        grow.addWidget(self.gem_key, 1)
        self.gem_model = QLineEdit(self.gem.get("model") or ai.DEFAULT_FALLBACKS[0]["model"])
        self.gem_model.setToolTip("Модель Gemini; gemini-flash-latest — всегда свежая бесплатная Flash")
        self.gem_model.setMaximumWidth(170)
        grow.addWidget(self.gem_model)
        g.addRow("Gemini:", grow)
        self.pol_on = QCheckBox("Pollinations — без ключа, модель попроще (GPT-OSS 20B), "
                                "текст уходит анонимно на их сервер")
        self.pol_on.setChecked(bool(self.pol.get("enabled", True)))
        g.addRow("", self.pol_on)
        v.addLayout(g)
        gnote = QLabel("У бесплатного Gemini Google может использовать присланный текст для улучшения "
                       "своих моделей. «Проверить связь» проверяет все включённые нейросети по очереди.")
        gnote.setWordWrap(True)
        gnote.setStyleSheet("color: gray;")
        v.addWidget(gnote)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        v.addStretch(1)
        row = QHBoxLayout()
        self.btn_test = QPushButton("Проверить связь")
        self.btn_test.clicked.connect(self._test)
        row.addWidget(self.btn_test)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        v.addLayout(row)

    def _current(self) -> dict:
        gem = dict(self.gem, enabled=self.gem_on.isChecked(), key=self.gem_key.text().strip(),
                   model=self.gem_model.text().strip() or ai.DEFAULT_FALLBACKS[0]["model"])
        pol = dict(self.pol, enabled=self.pol_on.isChecked())
        return {"url": self.url.text().strip() or ai.DEFAULT_URL, "key": self.key.text().strip(),
                "model": (self.model.currentText().strip() or ai.DEFAULT_MODEL).split("   (")[0],
                "fallbacks": [gem, pol]}

    def _run(self, fn, done, what):
        if self.thread and self.thread.isRunning():
            return
        self.status.setText(what)
        th = FuncThread(lambda p, s: fn(), parent=self, name=what)
        th.done.connect(done)
        th.failed.connect(lambda e: self.status.setText("❌ " + e.split(": ", 1)[-1]))
        self.thread = th
        th.start()

    def _load_models(self):
        cfg = self._current()

        def done(models):
            cur = self.model.currentText()
            self.model.clear()
            for mid, free in models:
                self.model.addItem(mid + ("" if free else "   (платная)"), mid)
            i = self.model.findData(cur)
            self.model.setCurrentIndex(i if i >= 0 else 0)
            if i < 0:
                self.model.setEditText(cur)
            self.status.setText(f"Моделей: {len(models)}, бесплатных: {sum(f for _, f in models)}.")
        self._run(lambda: ai.list_models(cfg), done, "Загружаю список моделей…")

    def _test(self):
        provs = ai.providers(self._current())
        if not provs:
            self.status.setText("Не задано ни одного ключа и Pollinations выключена.")
            return

        def check_all():
            res = []
            for pr in provs:
                t = time.time()
                try:
                    ans, model = ai.chat(pr, [{"role": "user", "content": "Ответь одним словом: работает"}],
                                         timeout=300, max_tokens=4000)
                    res.append(f"✔ {pr['name']}: «{ans.strip()[:40]}» за {time.time() - t:.0f} с ({model})")
                except ai.AIError as e:
                    res.append(f"❌ {pr['name']}: {e}")
            return res

        self._run(check_all, lambda res: self.status.setText("\n".join(res)),
                  f"Проверяю {len(provs)} шт. по очереди… Каждая может думать до минуты.")

    def _save(self):
        c = self._current()
        self.cfg.update(c)
        self.settings.save()
        fb = ", ".join(f"{x['name']}{'' if x.get('enabled') else ' (выкл.)'}" for x in c["fallbacks"])
        log.info(f"Нейросеть: настройки сохранены, модель {c['model']}, ключ {'есть' if c['key'] else 'нет'}, "
                 f"запасные: {fb}, ключ Gemini {'есть' if c['fallbacks'][0].get('key') else 'нет'}")
        self.accept()

    def reject(self):
        if self.thread and self.thread.isRunning():
            self.thread.wait(200)
        super().reject()


class AIPanel(QWidget):
    HISTORY_TURNS = 6          # сколько прошлых просьб помнить

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.thread: FuncThread | None = None
        self.scanning = False
        self.answer: ai.Answer | None = None
        self.undo_stack: list[dict] = []
        self.answer_doc = None
        self.started = 0.0
        self.phase = "Думаю…"
        # разговор: реплики для нейросети и что человек сделал с последним ответом
        self.history: list[dict] = []
        self.history_doc = None
        self.pending_notes: list[str] = []

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.lbl_model = QLabel("")
        self.lbl_model.setStyleSheet("color: gray;")
        top.addWidget(self.lbl_model, 1)
        self.btn_new = QPushButton("Новый разговор")
        self.btn_new.setToolTip("Помощник забудет прошлые просьбы и ответы")
        self.btn_new.clicked.connect(self.new_chat)
        top.addWidget(self.btn_new)
        b = QPushButton("Настройки…")
        b.clicked.connect(mw.ai_settings)
        top.addWidget(b)
        v.addLayout(top)

        ex = QLabel(EXAMPLES)
        ex.setWordWrap(True)
        ex.setStyleSheet("color: gray;")
        v.addWidget(ex)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Что сделать с текстом, произношением или настройками?")
        self.input.setFixedHeight(90)
        v.addWidget(self.input)
        row = QHBoxLayout()
        self.btn_ask = QPushButton("Спросить")
        self.btn_ask.setMinimumHeight(34)
        self.btn_ask.clicked.connect(self.ask)
        row.addWidget(self.btn_ask)
        self.btn_scan = QPushButton("Произношение во всей книге…")
        self.btn_scan.setToolTip("Помощник прочитает книгу частями и предложит, как читать иностранные "
                                 "имена, названия, редкие слова и аббревиатуры")
        self.btn_scan.clicked.connect(self.scan)
        row.addWidget(self.btn_scan)
        self.btn_stop = QPushButton("Остановить")
        self.btn_stop.setToolTip("Перестать ждать ответ (уже отправленный запрос на сервере не отменить)")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_stop)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        row.addWidget(self.lbl_status, 1)
        v.addLayout(row)
        QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self.ask)
        QShortcut(QKeySequence("Ctrl+Enter"), self.input, activated=self.ask)

        self.reply = QTextBrowser()
        self.reply.setPlaceholderText("Здесь будет разговор с помощником.")
        self.reply.setMinimumHeight(80)
        self.reply.setMaximumHeight(200)
        v.addWidget(self.reply, 1)
        v.addWidget(QLabel("Что предлагается (снимите галочку, если не нужно):"))
        self.props = QListWidget()
        self.props.setWordWrap(True)
        self.props.setAlternatingRowColors(True)
        self.props.setSpacing(2)
        self.props.setMinimumHeight(140)
        v.addWidget(self.props, 3)
        row2 = QHBoxLayout()
        self.btn_apply = QPushButton("Применить отмеченное")
        self.btn_apply.setMinimumHeight(34)
        self.btn_apply.clicked.connect(self.apply)
        self.btn_apply.setEnabled(False)
        row2.addWidget(self.btn_apply)
        self.btn_all = QPushButton("Отметить всё")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        row2.addWidget(self.btn_all)
        self.btn_none = QPushButton("Снять всё")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        row2.addWidget(self.btn_none)
        self.btn_undo = QPushButton("Отменить применённое")
        self.btn_undo.setToolTip("Вернуть правила, словарь произношения, главы, пропущенные куски, "
                                 "исправленные строки и настройки как было до последнего «Применить»")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_undo.setEnabled(False)
        row2.addWidget(self.btn_undo)
        row2.addStretch(1)
        v.addLayout(row2)

        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self._tick)
        self.refresh()

    def refresh(self):
        cfg = ai.config(self.mw.settings)
        names = [p["name"] if p["name"] != "основная" else p["model"] for p in ai.providers(cfg)]
        self.lbl_model.setText("Модель: " + (" → ".join(names) or "не задана"))

    # ------------------------------------------------------------ разговор
    def new_chat(self):
        self.history.clear()
        self.pending_notes.clear()
        self.history_doc = None
        self.reply.clear()
        self.lbl_status.setText("Начат новый разговор.")

    def _say(self, who: str, text: str):
        """Дописать реплику в окно разговора."""
        esc = html.escape(text).replace("\n", "<br>")
        color = "#2a6db0" if who == "Вы" else "#3a7d3a"
        self.reply.append(f"<p><b style='color:{color}'>{who}:</b> {esc}</p>")
        self.reply.verticalScrollBar().setValue(self.reply.verticalScrollBar().maximum())

    def _history_for(self, doc) -> list[dict]:
        if doc is not self.history_doc:          # другая книга — прошлый разговор о ней не нужен
            self.history.clear()
            self.pending_notes.clear()
            self.history_doc = doc
        return list(self.history[-2 * self.HISTORY_TURNS:])

    def _env(self) -> dict:
        """Настройки и голоса — для раздела «Настройки» в запросе и проверки предложений."""
        from ..audio import EXPORT_FORMATS, QUALITY_LEVELS, SPLIT_MODES
        from ..engines import ENGINES
        s = self.mw.settings
        engines = {}
        for eid, cls in ENGINES.items():
            try:
                # import torch занимает секунды — для Silero только проверяем, что он есть
                ok = (importlib.util.find_spec("torch") is not None) if eid == "silero" \
                    else bool(cls.available()[0])
                voices = [(vi.id, vi.name) for vi in cls.list_voices()] if ok else []
            except Exception:
                ok, voices = False, []
            engines[eid] = {"title": cls.title, "available": ok, "voices": voices}
        return {"engine": s["engine"], "voices": dict(s["voices"]), "engines": engines,
                "params": dict(s["params"]), "export": dict(s["export"]),
                "text": {k: v for k, v in s["text"].items() if k != "replacements"},
                "choices": {"export_format": list(EXPORT_FORMATS), "export_quality": list(QUALITY_LEVELS),
                            "split_mode": list(SPLIT_MODES), "auto_on_off": ["auto", "on", "off"]},
                "labels": {"export_format": {k: v[2] for k, v in EXPORT_FORMATS.items()},
                           "split_mode": dict(SPLIT_MODES),
                           "numbers": {"auto": "авто", "on": "всегда", "off": "никогда"},
                           "translit": {"auto": "авто", "on": "всегда", "off": "никогда"}}}

    # ------------------------------------------------------------ запрос
    def _busy(self) -> bool:
        return bool(self.thread and self.thread.isRunning())

    def _start(self, fn, name: str, phase: str, on_done):
        th = FuncThread(fn, parent=self, name=name)
        th.progress.connect(lambda a, th=th: self._progress(th, a))
        th.done.connect(lambda ans, th=th: on_done(th, ans))
        th.failed.connect(lambda err, th=th: self._failed(th, err))
        self.thread = th
        self.started = time.time()
        self.phase = phase
        for b in (self.btn_ask, self.btn_scan, self.btn_apply):
            b.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.props.clear()
        self.lbl_status.setText(phase)
        self.tick.start()
        th.start()

    def ask(self):
        cmd = self.input.toPlainText().strip()
        if not cmd or self._busy():
            return
        mw = self.mw
        selected = mw._selected_lines() if mw.doc else []
        ctx = ai.build_context(mw.doc, mw.status, selected, mw.settings["filter"],
                               mw.settings["text"]["replacements"], ai.wants_headings(cmd))
        cfg = copy.deepcopy(ai.config(mw.settings))
        doc, flt, text = mw.doc, copy.deepcopy(mw.settings["filter"]), mw.editor.toPlainText()
        status = list(mw.status) if mw.doc else None
        history = self._history_for(doc)
        notes = self.pending_notes[:]
        full_cmd = cmd + ("\n\n(После твоего прошлого ответа: " + "; ".join(notes) + ")" if notes else "")
        env = self._env()
        self.answer_doc = doc
        self._say("Вы", cmd)
        self.input.clear()
        self.scanning = False
        self._start(lambda p, s: ai.run(cfg, full_cmd, ctx, doc, flt, text, status, history, env,
                                        progress=lambda msg: p(msg), stop_flag=s),
                    "запрос к нейросети",
                    "Думаю…" + (f" (смотрю на выделенные строки: {len(selected)})" if selected else ""),
                    lambda th, ans, c=full_cmd: self._done(th, ans, c))

    def scan(self):
        mw = self.mw
        if self._busy():
            return
        if not mw.doc:
            QMessageBox.information(self, "Помощник", "Сначала откройте книгу.")
            return
        selected = mw._selected_lines()
        included = [mw.lines_model.included(i) for i in range(len(mw.doc.lines))]
        chunks = ai.book_chunks(mw.doc, included, only=selected or None)
        if not chunks:
            QMessageBox.information(self, "Помощник", "В книге нет текста для чтения.")
            return
        tokens = ai.estimate_tokens(chunks)
        what = f"выделенные строки ({len(selected)})" if selected else "всю книгу"
        if QMessageBox.question(
                self, "Произношение во всей книге",
                f"Помощник прочитает {what} и предложит, как читать иностранные имена, названия, "
                f"редкие слова и аббревиатуры.\n\n"
                f"Запросов к нейросети: {len(chunks)}, примерно {tokens // 1000} тыс. токенов.\n"
                f"Весь этот текст уйдёт на сервер нейросети. На платной модели это стоит денег, "
                f"бесплатная может упереться в лимит — тогда покажу то, что успел найти.\n\n"
                f"Начать?") != QMessageBox.Yes:
            return
        cfg = copy.deepcopy(ai.config(mw.settings))
        reps = copy.deepcopy(mw.settings["text"]["replacements"])
        text = mw.editor.toPlainText()
        self.answer_doc = mw.doc
        self.scanning = True
        self._say("Вы", f"Проверь произношение: {what}")
        self._start(lambda p, s: ai.scan_pronunciation(cfg, chunks, reps, text,
                                                       progress=lambda msg: p(msg), stop_flag=s),
                    "проход по книге", f"Читаю книгу: часть 1 из {len(chunks)}",
                    lambda th, ans: self._done(th, ans, None))

    def _progress(self, th, a):
        if th is self.thread and a:
            self.phase = str(a[0])
            self._tick()

    def _tick(self):
        self.lbl_status.setText(f"{self.phase} {time.time() - self.started:.0f} с")

    def _finish(self):
        self.tick.stop()
        self.btn_ask.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def stop(self):
        th = self.thread
        if th is None:
            return
        th.cancel()
        if self.scanning and th.isRunning():
            # проход по книге остановится после текущей части и покажет, что успел найти
            self.phase = "Останавливаюсь после текущей части…"
            self.btn_stop.setEnabled(False)
            return
        self.thread = None       # ответ этого запроса будет проигнорирован
        self._finish()
        self.lbl_status.setText("Не ждём ответа.")

    def _failed(self, th, err):
        if th is not self.thread:
            return
        self._finish()
        msg = err.split(": ", 1)[1] if err.startswith(("AIError: ", "TransientError: ")) else err
        self.lbl_status.setText("")
        self._say("Помощник", "Не получилось: " + msg)

    def _done(self, th, ans: ai.Answer, cmd: str | None):
        if th is not self.thread:
            return
        self._finish()
        self.answer = ans
        if cmd is not None:      # обычная просьба — запоминаем в разговоре
            self.history += [{"role": "user", "content": "Просьба: " + cmd},
                             {"role": "assistant", "content": ans.memory}]
            self.pending_notes.clear()
        extra = f", запросов: {ans.requests}" if ans.requests > 1 else ""
        self.lbl_status.setText(f"Ответ за {ans.seconds:.0f} с{extra}" + (" (запасная нейросеть)" if ans.via else ""))
        text = ans.reply or "(без пояснений)"
        if ans.looked:
            text += "\n\nПосмотрел в книге: " + "; ".join(ans.looked[:12]) + ("…" if len(ans.looked) > 12 else "")
        if ans.via:
            text += f"\n\n[{ans.via}]"
        self._say("Помощник", text)
        self.props.clear()
        for p in ans.proposals:
            lines = [p.title]
            if p.detail:
                lines.append(p.detail)
            lines += ["   • " + s for s in p.samples]
            it = QListWidgetItem("\n".join(lines))
            if p.ok:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if p.checked else Qt.Unchecked)
            else:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            it.setData(Qt.UserRole, p)
            self.props.addItem(it)
        self.btn_apply.setEnabled(any(p.ok for p in ans.proposals))

    def _check_all(self, on: bool):
        for i in range(self.props.count()):
            it = self.props.item(i)
            if it.flags() & Qt.ItemIsUserCheckable and it.flags() & Qt.ItemIsEnabled:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    # ------------------------------------------------------------ применение
    def apply(self):
        mw = self.mw
        chosen, declined = [], []
        for i in range(self.props.count()):
            it = self.props.item(i)
            p = it.data(Qt.UserRole)
            if p.ok and it.checkState() == Qt.Checked and it.flags() & Qt.ItemIsEnabled:
                chosen.append((it, p))
            elif p.ok and it.flags() & Qt.ItemIsEnabled:
                declined.append(p.title)
        if not chosen:
            return
        book_ok = mw.doc is not None and mw.doc is self.answer_doc
        s = mw.settings
        flt, reps = s["filter"], s["text"]["replacements"]
        self.undo_stack.append({
            "filter": copy.deepcopy(flt), "reps": copy.deepcopy(reps), "doc": mw.doc,
            "overrides": dict(mw.overrides), "chapters": list(mw.doc.chapters) if mw.doc else [],
            "engine": s["engine"], "voices": copy.deepcopy(s["voices"]), "params": copy.deepcopy(s["params"]),
            "export": copy.deepcopy(s["export"]),
            "text": {k: copy.deepcopy(v) for k, v in s["text"].items() if k != "replacements"},
            "lines": {}})
        snap = self.undo_stack[-1]
        done, skipped, notes, applied = 0, 0, [], []
        cut_lines: list[int] = []
        edited, settings_changed = 0, False
        for it, p in chosen:
            d = p.data
            if d["type"] in ("chapters", "delete", "edit", "rename_chapter") and not book_ok:
                skipped += 1        # номера строк относятся к другой книге
                continue
            if d["type"] == "rule":
                flt.setdefault("rules", []).append(copy.deepcopy(d["rule"]))
            elif d["type"] == "option":
                flt[d["key"]] = d["value"]
            elif d["type"] == "replace":
                reps.append(copy.deepcopy(d["entry"]))
            elif d["type"] == "chapters":
                mw.doc.chapters = [Chapter(t, i, 1) for i, t in d["chapters"]]
                notes.append(f"глав теперь {len(d['chapters'])}")
            elif d["type"] == "delete":
                cut_lines += range(d["from"], d["to"] + 1)
            elif d["type"] == "edit":
                ln = mw.doc.lines[d["line"]]
                if "text" in d:
                    new = d["text"]
                elif d["find"] in ln.text:
                    new = ln.text.replace(d["find"], d["replace"], 1)
                else:
                    skipped += 1    # строку уже поменяла другая правка
                    continue
                snap["lines"].setdefault(d["line"], ln.text)
                ln.text = new
                edited += 1
            elif d["type"] == "rename_chapter":
                mw.doc.chapters = [Chapter(d["title"], c.line, c.level) if c.line == d["line"] else c
                                   for c in mw.doc.chapters]
            elif d["type"] == "setting":
                if d["sec"] == "":
                    s[d["field"]] = d["value"]
                else:
                    s[d["sec"]][d["field"]] = d["value"]
                settings_changed = True
            done += 1
            applied.append(p.title)
            it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            it.setText("✔ " + it.text())
        log.info(f"Нейросеть: применено предложений: {done}" + (f", пропущено: {skipped}" if skipped else ""))
        if settings_changed:
            mw._load_settings_to_ui()      # голос, скорость, экспорт, обработка текста — в окно
        self._settings_changed()
        if cut_lines:
            # ручная отметка «не читать» — как из меню на вкладке «Что читать»
            mw.lines_model.set_override(sorted(set(cut_lines)), False)
            notes.append(f"не читать строк: {len(set(cut_lines))} (отметка «вручную» на вкладке «Что читать»)")
        if edited:
            notes.append(f"исправлено строк: {edited} (до повторного открытия книги)")
        mw._update_chapters_label()
        self.btn_apply.setEnabled(False)
        self.btn_undo.setEnabled(True)
        # помощнику в следующей просьбе — что из его ответа принято
        self.pending_notes = ["человек применил: " + "; ".join(applied[:20])]
        if declined:
            self.pending_notes.append("не стал применять: " + "; ".join(declined[:20]))
        msg = f"Применено: {done}."
        if notes:
            t = "; ".join(notes)
            msg += " " + t[:1].upper() + t[1:] + "."
        if skipped:
            msg += f" Пропущено: {skipped} (открыта другая книга или строка уже изменена)."
        if mw.manual_text and (cut_lines or edited or any(p.data["type"] == "chapters" for _, p in chosen)):
            msg += " Включена правка текста вручную — уберите галочку, чтобы текст пересобрался."
        self.lbl_status.setText(msg)

    def undo(self):
        if not self.undo_stack:
            return
        snap = self.undo_stack.pop()
        mw = self.mw
        s = mw.settings
        s["filter"] = snap["filter"]
        s["text"]["replacements"] = snap["reps"]
        s["engine"], s["voices"] = snap["engine"], snap["voices"]
        s["params"], s["export"] = snap["params"], snap["export"]
        s["text"].update(snap["text"])
        mw._load_settings_to_ui()
        same_doc = mw.doc is not None and mw.doc is snap["doc"]
        if same_doc:
            for i, old in snap["lines"].items():
                mw.doc.lines[i].text = old
        self._settings_changed()
        if same_doc:
            mw.doc.chapters = snap["chapters"]
            mw.overrides.clear()
            mw.overrides.update(snap["overrides"])
            mw.lines_model.set_data(mw.doc, mw.status, mw.overrides)
            mw.schedule_rebuild()
            mw._update_chapters_label()
        self.btn_undo.setEnabled(bool(self.undo_stack))
        self.pending_notes = ["человек отменил всё, что применил из твоего прошлого ответа"]
        self.lbl_status.setText("Отменено: всё вернулось как было до «Применить».")
        log.info("Нейросеть: применённое отменено")

    def _settings_changed(self):
        mw = self.mw
        mw._filter_to_ui()
        mw._fill_repl_table()
        mw.settings.save()
        mw._filter_changed()
