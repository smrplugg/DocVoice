"""Диалог управления голосами: загрузка Piper-голосов, моделей Silero, список голосов Windows."""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import accent
from ..audio import download
from ..engines import (SILERO_MODELS, PiperEngine, SapiEngine, SileroEngine)
from ..paths import piper_dir, silero_dir
from .common import FuncThread

# если voices.json недоступен — показываем хотя бы этот список
FALLBACK_PIPER = [
    "ru_RU-irina-medium", "ru_RU-denis-medium", "ru_RU-dmitri-medium", "ru_RU-ruslan-medium",
    "en_US-lessac-medium", "en_US-amy-medium", "en_US-ryan-high", "en_US-libritts_r-medium",
    "en_GB-alan-medium", "en_GB-cori-high", "uk_UA-ukrainian_tts-medium", "uk_UA-lada-x_low",
    "de_DE-thorsten-medium", "fr_FR-siwis-medium", "es_ES-davefx-medium", "kk_KZ-issai-high",
]


def _piper_rel(key: str) -> str:
    code, name, quality = key.split("-", 2)
    family = code.split("_")[0]
    return f"{family}/{code}/{name}/{quality}/{key}"


def _fmt_size(n: int) -> str:
    if not n:
        return "?"
    return f"{n / 1024 / 1024:.0f} МБ"


class VoicesDialog(QDialog):
    def __init__(self, settings, parent=None, tab: str | None = None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Управление голосами")
        self.resize(820, 560)
        self.thread: FuncThread | None = None
        self.changed = False

        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.tabs.addTab(self._piper_tab(), "Piper")
        self.tabs.addTab(self._silero_tab(), "Silero")
        self.tabs.addTab(self._sapi_tab(), "Голоса Windows")
        self.accent_tab = self._accent_tab()
        self.tabs.addTab(self.accent_tab, "Умные ударения")
        if tab == "accent":
            self.tabs.setCurrentWidget(self.accent_tab)

        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.cancel_btn = QPushButton("Отмена загрузки")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(close)
        lay.addLayout(bottom)

        self._load_piper_catalog(online=False)

    # ------------------------------------------------------------------ Piper
    def _piper_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        ok, msg = PiperEngine.available()
        if not ok:
            lbl = QLabel(f"<b>Piper недоступен:</b> {msg}")
            lbl.setWordWrap(True)
            v.addWidget(lbl)
        info = QLabel("Голоса скачиваются с HuggingFace (rhasspy/piper-voices). Для русского "
                      "рекомендуются <b>irina</b>, <b>denis</b>, <b>dmitri</b>, <b>ruslan</b> (medium). "
                      "Если HuggingFace недоступен, укажите зеркало, например https://hf-mirror.com.")
        info.setWordWrap(True)
        v.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Сервер:"))
        self.mirror = QLineEdit(self.settings["piper_mirror"])
        row.addWidget(self.mirror, 1)
        row.addWidget(QLabel("Язык:"))
        self.lang = QComboBox()
        self.lang.setEditable(True)
        self.lang.addItems(["ru", "en", "uk", "de", "fr", "es", "kk", "все"])
        self.lang.setCurrentText(self.settings["piper_lang_filter"])
        self.lang.currentTextChanged.connect(lambda _: self._fill_piper_table())
        row.addWidget(self.lang)
        refresh = QPushButton("Обновить список")
        refresh.clicked.connect(lambda: self._load_piper_catalog(online=True))
        row.addWidget(refresh)
        v.addLayout(row)

        self.ptable = QTableWidget(0, 5)
        self.ptable.setHorizontalHeaderLabels(["Голос", "Язык", "Качество", "Размер", "Статус"])
        self.ptable.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ptable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ptable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ptable.verticalHeader().setVisible(False)
        v.addWidget(self.ptable, 1)

        btns = QHBoxLayout()
        b1 = QPushButton("Скачать выбранные")
        b1.clicked.connect(self._download_piper)
        b2 = QPushButton("Удалить выбранные")
        b2.clicked.connect(self._delete_piper)
        b3 = QPushButton("Добавить свой .onnx…")
        b3.clicked.connect(self._add_onnx)
        b4 = QPushButton("Открыть папку")
        b4.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(piper_dir()))))
        for b in (b1, b2, b3, b4):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)
        self.catalog: dict = {}
        return w

    def _catalog_path(self):
        return piper_dir() / "voices.json"

    def _load_piper_catalog(self, online: bool):
        cp = self._catalog_path()
        if not online and cp.exists():
            try:
                self.catalog = json.loads(cp.read_text(encoding="utf-8"))
                self._fill_piper_table()
                return
            except Exception:
                pass
        if not online:
            self.catalog = {k: {"key": k, "language": {"code": k.split("-")[0]},
                                "quality": k.split("-", 2)[2], "files": {}} for k in FALLBACK_PIPER}
            self._fill_piper_table()
            self.status.setText("Показан встроенный список. Нажмите «Обновить список» для полного каталога.")
            return
        mirror = self.mirror.text().strip().rstrip("/")
        self.settings["piper_mirror"] = mirror
        url = f"{mirror}/rhasspy/piper-voices/resolve/main/voices.json?download=true"

        def job(progress, stop):
            return download(url, cp, progress, stop)

        self._run(job, self._on_catalog_loaded, "Загрузка каталога…")

    def _on_catalog_loaded(self, _):
        try:
            self.catalog = json.loads(self._catalog_path().read_text(encoding="utf-8"))
            self.status.setText(f"Голосов в каталоге: {len(self.catalog)}")
        except Exception as e:
            self.status.setText(f"Ошибка каталога: {e}")
        self._fill_piper_table()

    def _fill_piper_table(self):
        lang = self.lang.currentText().strip().lower()
        self.settings["piper_lang_filter"] = lang
        installed = {p.stem for p in piper_dir().glob("*.onnx")}
        keys = sorted(self.catalog)
        if lang and lang != "все":
            keys = [k for k in keys if k.lower().startswith(lang)]
        # установленные голоса, которых нет в каталоге (свои)
        extra = sorted(installed - set(self.catalog))
        self.ptable.setRowCount(0)
        for k in keys + extra:
            info = self.catalog.get(k, {})
            r = self.ptable.rowCount()
            self.ptable.insertRow(r)
            parts = k.split("-", 2)
            size = sum(f.get("size_bytes", 0) for n, f in (info.get("files") or {}).items()
                       if n.endswith(".onnx"))
            nspk = info.get("num_speakers", 1)
            name = parts[1] if len(parts) == 3 else k
            if nspk and nspk > 1:
                name += f"  ({nspk} голосов)"
            items = [name, (info.get("language") or {}).get("code", parts[0]),
                     parts[2] if len(parts) == 3 else "", _fmt_size(size),
                     "✔ установлен" if k in installed else ""]
            for c, t in enumerate(items):
                it = QTableWidgetItem(t)
                it.setData(Qt.UserRole, k)
                self.ptable.setItem(r, c, it)

    def _selected_keys(self, table) -> list[str]:
        rows = sorted({i.row() for i in table.selectedIndexes()})
        return [table.item(r, 0).data(Qt.UserRole) for r in rows]

    def _download_piper(self):
        keys = self._selected_keys(self.ptable)
        if not keys:
            QMessageBox.information(self, "Голоса", "Выберите голоса в таблице.")
            return
        mirror = self.mirror.text().strip().rstrip("/")
        self.settings["piper_mirror"] = mirror

        def job(progress, stop):
            for n, k in enumerate(keys):
                rel = _piper_rel(k)
                for ext in (".onnx.json", ".onnx"):
                    url = f"{mirror}/rhasspy/piper-voices/resolve/main/{rel}{ext}?download=true"
                    dest = piper_dir() / f"{k}{ext}"
                    if dest.exists() and dest.stat().st_size > 0:
                        continue
                    download(url, dest,
                             lambda got, total, k=k, n=n: progress(f"{k} ({n + 1}/{len(keys)})", got, total),
                             stop)
            return len(keys)

        self._run(job, lambda _: (self._fill_piper_table(), self._mark_changed(),
                                  self.status.setText("Готово")), "Загрузка…")

    def _delete_piper(self):
        keys = self._selected_keys(self.ptable)
        if not keys:
            return
        if QMessageBox.question(self, "Удаление", f"Удалить голоса: {', '.join(keys)}?") != QMessageBox.Yes:
            return
        for k in keys:
            for ext in (".onnx", ".onnx.json"):
                (piper_dir() / f"{k}{ext}").unlink(missing_ok=True)
        self._mark_changed()
        self._fill_piper_table()

    def _add_onnx(self):
        path, _ = QFileDialog.getOpenFileName(self, "Файл голоса Piper", "", "Piper (*.onnx)")
        if not path:
            return
        cfg = Path(path + ".json")
        if not cfg.exists():
            alt = Path(path).with_suffix(".json")
            if alt.exists():
                cfg = alt
            else:
                QMessageBox.warning(self, "Голос", "Рядом с .onnx должен лежать файл конфигурации "
                                                   "<имя>.onnx.json")
                return
        dst = piper_dir() / Path(path).name
        shutil.copy2(path, dst)
        shutil.copy2(cfg, dst.with_suffix(".onnx.json"))
        self._mark_changed()
        self._fill_piper_table()

    # ----------------------------------------------------------------- Silero
    def _silero_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        ok, msg = SileroEngine.available()
        lbl = QLabel(SileroEngine.description + ("" if ok else f"<br><b>Внимание:</b> {msg}"))
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        self.stable = QTableWidget(0, 3)
        self.stable.setHorizontalHeaderLabels(["Модель", "Описание", "Статус"])
        self.stable.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.stable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.stable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.stable.verticalHeader().setVisible(False)
        v.addWidget(self.stable, 1)
        btns = QHBoxLayout()
        b1 = QPushButton("Скачать выбранную")
        b1.clicked.connect(self._download_silero)
        b2 = QPushButton("Удалить")
        b2.clicked.connect(self._delete_silero)
        b3 = QPushButton("Добавить свой .pt…")
        b3.clicked.connect(self._add_pt)
        for b in (b1, b2, b3):
            btns.addWidget(b)
        btns.addStretch(1)
        v.addLayout(btns)
        note = QLabel("Голоса: aidar, eugene (муж.), baya, kseniya, xenia (жен.). "
                      "Ударения программа может ставить сама — вкладка «Умные ударения». Вручную: "
                      "знак «+» перед гласной в словаре замен, «з+амок» / «зам+ок».")
        note.setWordWrap(True)
        v.addWidget(note)
        self._fill_silero()
        return w

    def _fill_silero(self):
        self.stable.setRowCount(0)
        for mid, (url, desc) in SILERO_MODELS.items():
            r = self.stable.rowCount()
            self.stable.insertRow(r)
            p = silero_dir() / f"{mid}.pt"
            for c, t in enumerate([mid, desc, f"✔ {p.stat().st_size // 1048576} МБ" if p.exists() else ""]):
                it = QTableWidgetItem(t)
                it.setData(Qt.UserRole, mid)
                self.stable.setItem(r, c, it)
        if self.stable.rowCount():
            self.stable.selectRow(0)

    def _download_silero(self):
        keys = self._selected_keys(self.stable)
        if not keys:
            return

        def job(progress, stop):
            for k in keys:
                url = SILERO_MODELS[k][0]
                download(url, silero_dir() / f"{k}.pt",
                         lambda got, total, k=k: progress(k, got, total), stop, timeout=60)

        self._run(job, lambda _: (self._fill_silero(), self._mark_changed(),
                                  self.status.setText("Готово")), "Загрузка…")

    def _delete_silero(self):
        for k in self._selected_keys(self.stable):
            (silero_dir() / f"{k}.pt").unlink(missing_ok=True)
        self._fill_silero()
        self._mark_changed()

    def _add_pt(self):
        path, _ = QFileDialog.getOpenFileName(self, "Модель Silero", "", "Silero (*.pt)")
        if not path:
            return
        name = Path(path).stem
        if name not in SILERO_MODELS:
            QMessageBox.warning(self, "Silero", "Имя файла должно совпадать с одной из моделей: "
                                + ", ".join(SILERO_MODELS) + " (например v5_ru.pt)")
            return
        shutil.copy2(path, silero_dir() / f"{name}.pt")
        self._fill_silero()
        self._mark_changed()

    # ---------------------------------------------------------- ударения
    def _accent_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel(
            "<b>Умные ударения</b> — программа сама ставит ударения и букву «ё» по смыслу "
            "предложения: «открыл зам<u>о</u>к» и «старинный з<u>а</u>мок», «на кухне была мук<u>а</u>» "
            "и «настоящая м<u>у</u>ка», «все пришли» и «всё хорошо».<br><br>"
            "Работает с голосами Silero и Piper, без интернета. Нужно один раз скачать "
            f"около {accent.TOTAL_BYTES // 1_000_000} МБ. Пока программа открыта, занимает "
            "около 600 МБ оперативной памяти; запись книги идёт примерно на 15% дольше.<br><br>"
            "Если где-то ударение всё равно неверное — исправьте слово в словаре замен на вкладке "
            "«Произношение»: «з+амок». Ваше исправление важнее.")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        self.accent_status = QLabel()
        self.accent_status.setWordWrap(True)
        v.addWidget(self.accent_status)
        row = QHBoxLayout()
        self.accent_dl = QPushButton("Скачать")
        self.accent_dl.setMinimumHeight(36)
        self.accent_dl.clicked.connect(self._download_accent)
        self.accent_rm = QPushButton("Удалить")
        self.accent_rm.clicked.connect(self._delete_accent)
        row.addWidget(self.accent_dl)
        row.addWidget(self.accent_rm)
        row.addStretch(1)
        v.addLayout(row)
        credit = QLabel("Модели и код: RUAccent (Denis Petrov), лицензия MIT.")
        credit.setStyleSheet("color: gray;")
        v.addWidget(credit)
        v.addStretch(1)
        self._fill_accent()
        return w

    def _fill_accent(self):
        if accent.installed():
            self.accent_status.setText(f"✔ Скачано ({accent.installed_size() // 1_000_000} МБ). "
                                       f"Включается галочкой на вкладке «Произношение» — по умолчанию включено.")
            self.accent_dl.setText("Скачать заново")
        else:
            self.accent_status.setText("Не скачано.")
            self.accent_dl.setText(f"Скачать (около {accent.TOTAL_BYTES // 1_000_000} МБ)")
        self.accent_rm.setEnabled(accent.accent_dir().exists())

    def _download_accent(self):
        mirror = self.mirror.text().strip() or self.settings["piper_mirror"]
        if accent.installed():
            accent.delete_models()

        def job(progress, stop):
            accent.download_models(mirror, lambda label, got, total: progress(label, got, total), stop)

        def done(_):
            self._fill_accent()
            self._mark_changed()
            self.status.setText("Готово: умные ударения включены")
            accent.preload_async()

        self._run(job, done, "Загрузка умных ударений…")

    def _delete_accent(self):
        if QMessageBox.question(self, "Умные ударения", "Удалить модели умных ударений?") == QMessageBox.Yes:
            accent.delete_models()
            self._fill_accent()
            self._mark_changed()

    # ------------------------------------------------------------------- SAPI
    def _sapi_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        ok, msg = SapiEngine.available()
        lbl = QLabel(SapiEngine.description + ("" if ok else f"<br><b>Недоступно:</b> {msg}"))
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        t = QTableWidget(0, 2)
        t.setHorizontalHeaderLabels(["Голос", "Язык"])
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for vi in SapiEngine.list_voices():
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem(vi.name))
            t.setItem(r, 1, QTableWidgetItem(vi.lang))
        v.addWidget(t, 1)
        b = QPushButton("Открыть параметры речи Windows")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("ms-settings:speech")))
        b.setEnabled(sys.platform == "win32")
        v.addWidget(b, 0, Qt.AlignLeft)
        return w

    # ---------------------------------------------------------------- general
    def _mark_changed(self):
        self.changed = True

    def _run(self, job, on_done, text):
        if self.thread is not None and self.thread.isRunning():
            QMessageBox.information(self, "Загрузка", "Дождитесь окончания текущей загрузки.")
            return
        self.status.setText(text)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.cancel_btn.setVisible(True)
        th = FuncThread(job, parent=self)
        th.progress.connect(self._on_progress)
        th.done.connect(lambda res: (self._finish(), on_done(res)))
        th.failed.connect(lambda err: (self._finish(), self._on_fail(err)))
        self.thread = th
        th.start()

    def _on_progress(self, args):
        if len(args) == 3:
            label, got, total = args
            self.status.setText(f"{label}: {got / 1048576:.1f} МБ")
        else:
            got, total = args
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(got / total * 1000))

    def _finish(self):
        self.progress.setVisible(False)
        self.cancel_btn.setVisible(False)

    def _on_fail(self, err):
        self.status.setText("Ошибка")
        if "Отменено" in err:
            self.status.setText("Отменено")
            return
        QMessageBox.warning(self, "Ошибка загрузки",
                            f"{err}\n\nПроверьте интернет или укажите другое зеркало. Файлы можно "
                            f"скачать вручную и добавить кнопкой «Добавить свой…».")

    def _cancel(self):
        if self.thread:
            self.thread.cancel()

    def closeEvent(self, e):
        if self.thread and self.thread.isRunning():
            self.thread.cancel()
            self.thread.wait(3000)
        super().closeEvent(e)

    def accept(self):
        if self.thread and self.thread.isRunning():
            self.thread.cancel()
            self.thread.wait(3000)
        super().accept()
