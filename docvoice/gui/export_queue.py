"""Очередь экспорта: задания выполняются по одному, у каждого — прогресс и оставшееся время."""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import textproc
from ..audio import export_audio
from ..extract import load_document
from ..log import describe_file, log
from .common import FuncThread

PENDING, PREPARING, RUNNING, DONE, FAILED, CANCELLED = (
    "ожидает", "подготовка", "идёт", "готово", "ошибка", "отменено")


@dataclass
class ExportJob:
    out_path: str
    fmt: str
    quality: float
    engine: str
    voice: str
    voice_name: str
    params: dict
    text_opts: dict
    split: float
    title: str
    artist: str
    texts: list[str] | None = None        # None — нужно извлечь из source
    source: str | None = None              # путь к документу (для заданий «из файлов»)
    filter_cfg: dict | None = None
    split_mode: str = "none"               # см. audio.SPLIT_MODES
    chapters: list | None = None           # [(индекс абзаца, название)]
    prior_rate: float | None = None        # скорость синтеза по прошлым запускам
    status: str = PENDING
    done_paras: int = 0
    total_paras: int = 0
    chars: int = 0
    done_chars: int = 0
    eta: float | None = None
    started: float = 0.0
    finished: float = 0.0
    files: list[str] = field(default_factory=list)
    error: str = ""

    def name(self) -> str:
        return Path(self.out_path).name


def fmt_time(sec: float | None) -> str:
    """«1 ч 23 мин», «12 мин», «меньше минуты»: без секунд, чтобы цифры не мельтешили."""
    if sec is None:
        return "—"
    if sec < 60:
        return "меньше минуты"
    m = int(round(sec / 60))
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


def _prepare(progress, stop, job: ExportJob):
    """Извлечение текста из документа с теми фильтрами, что были на момент добавления."""
    doc = load_document(job.source)
    st = textproc.evaluate_lines(doc, job.filter_cfg)
    paras = textproc.build_paragraphs(doc, [s.included for s in st], job.filter_cfg, job.text_opts)
    texts = [p.text for p in paras]
    job.chapters = textproc.chapter_starts(doc, paras)
    if not job.title:
        job.title = doc.title or Path(job.source).stem
    return texts


def _run(progress, stop, job: ExportJob):
    return export_audio(job.texts, job.out_path, job.fmt, job.quality, job.engine, job.voice,
                        job.params, job.text_opts, job.split, job.title, job.artist,
                        progress=lambda i, n, eta: progress(i, n, eta), stop_flag=stop,
                        split_mode=job.split_mode, chapters=job.chapters,
                        prior_rate=job.prior_rate)


class ExportQueue(QObject):
    """Логика очереди. Готовит задания (извлечение текста) и озвучивает по одному."""
    changed = Signal()
    jobFinished = Signal(object)
    allFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[ExportJob] = []
        self.run_thread: FuncThread | None = None
        self.prep_thread: FuncThread | None = None
        self.current: ExportJob | None = None
        # скорость синтеза (символов в секунду) по движку+голосу — для оценки ожидающих
        self.rates: dict[tuple[str, str], float] = {}

    # ---- управление
    def add(self, job: ExportJob):
        log.info(f"В очередь записи: {job.fmt}, {job.engine}"
                 + (f", из файла {describe_file(job.source)}" if job.source else f", абзацев {len(job.texts or [])}"))
        if job.texts is not None:
            job.total_paras = len(job.texts)
            job.chars = sum(len(t) for t in job.texts)
        else:
            job.status = PREPARING
        self.jobs.append(job)
        self.changed.emit()
        self._kick()

    def busy(self) -> bool:
        return any(j.status in (PENDING, PREPARING, RUNNING) for j in self.jobs)

    def cancel(self, job: ExportJob):
        if job.status == RUNNING and self.run_thread:
            self.run_thread.cancel()
            job.status = CANCELLED
        elif job.status in (PENDING, PREPARING):
            job.status = CANCELLED
        self.changed.emit()

    def cancel_all(self):
        for j in self.jobs:
            if j.status in (PENDING, PREPARING, RUNNING):
                self.cancel(j)

    def remove(self, job: ExportJob):
        if job.status in (PENDING, PREPARING, RUNNING):
            self.cancel(job)
        if job.status != RUNNING and job in self.jobs and job is not self.current:
            self.jobs.remove(job)
        self.changed.emit()

    def retry(self, job: ExportJob):
        if job.status in (FAILED, CANCELLED):
            job.status = PENDING if job.texts is not None else PREPARING
            job.error = ""
            job.done_paras = job.done_chars = 0
            job.eta = None
            self.changed.emit()
            self._kick()

    def move(self, job: ExportJob, delta: int):
        i = self.jobs.index(job)
        k = max(0, min(len(self.jobs) - 1, i + delta))
        self.jobs.insert(k, self.jobs.pop(i))
        self.changed.emit()

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.status in (PENDING, PREPARING, RUNNING)]
        self.changed.emit()

    def wait(self, ms: int = 5000):
        for th in (self.run_thread, self.prep_thread):
            if th and th.isRunning():
                th.wait(ms)

    # ---- оценки
    def rate_for(self, job: ExportJob) -> float | None:
        r = self.rates.get((job.engine, job.voice))
        if r is None and self.rates:
            r = min(self.rates.values())     # другой голос — берём осторожную оценку
        return r

    def remaining(self, job: ExportJob) -> float | None:
        if job.status == RUNNING:
            return job.eta
        if job.status in (PENDING,):
            r = self.rate_for(job)
            return job.chars / r if r else None
        return None

    def total_remaining(self) -> float | None:
        tot = 0.0
        for j in self.jobs:
            if j.status in (PENDING, RUNNING):
                r = self.remaining(j)
                if r is None:
                    return None
                tot += r
        return tot

    # ---- исполнение
    def _kick(self):
        # подготовка (по одной, параллельно с озвучкой)
        if not (self.prep_thread and self.prep_thread.isRunning()):
            job = next((j for j in self.jobs if j.status == PREPARING), None)
            if job:
                th = FuncThread(_prepare, job, parent=self, name="подготовка текста для записи")
                th.done.connect(lambda texts, j=job: self._prepared(j, texts))
                th.failed.connect(lambda err, j=job: self._prep_failed(j, err))
                self.prep_thread = th
                th.start()
        # озвучка
        if self.run_thread and self.run_thread.isRunning():
            return
        job = next((j for j in self.jobs if j.status == PENDING), None)
        if not job:
            if not self.busy():
                self.allFinished.emit()
            return
        job.status = RUNNING
        job.started = time.time()
        job.prior_rate = self.rates.get((job.engine, job.voice))
        job.done_paras = job.done_chars = 0
        self.current = job
        th = FuncThread(_run, job, parent=self, name="запись в аудиофайл")
        th.progress.connect(lambda a, j=job: self._progress(j, a))
        th.done.connect(lambda files, j=job: self._finished(j, files, None))
        th.failed.connect(lambda err, j=job: self._finished(j, [], err))
        self.run_thread = th
        th.start()
        self.changed.emit()

    def _prepared(self, job, texts):
        if job.status == CANCELLED:
            pass
        elif not texts:
            job.status = FAILED
            job.error = "в документе нет текста для озвучки"
        else:
            job.texts = texts
            job.total_paras = len(texts)
            job.chars = sum(len(t) for t in texts)
            job.status = PENDING
        self.changed.emit()
        self._kick()

    def _prep_failed(self, job, err):
        if job.status != CANCELLED:
            job.status = FAILED
            job.error = err
        self.changed.emit()
        self._kick()

    def _progress(self, job, args):
        i, n, eta = args
        job.done_paras = i
        job.total_paras = n
        job.done_chars = sum(len(t) for t in job.texts[:i])
        job.eta = eta
        job._eta_t = time.time()
        el = time.time() - job.started
        if el > 30 and job.done_chars:     # первые секунды — загрузка модели, не показательны
            self.rates[(job.engine, job.voice)] = job.done_chars / el
        self.changed.emit()

    def _finished(self, job, files, err):
        job.finished = time.time()
        self.current = None
        if err:
            log.error(f"Запись не удалась через {job.finished - job.started:.0f} с: {err}")
        if err:
            job.status = FAILED
            job.error = err
        elif job.status == CANCELLED or not files:
            job.status = CANCELLED
        else:
            job.status = DONE
            job.files = files
            el = job.finished - job.started
            if el > 1:
                self.rates[(job.engine, job.voice)] = job.chars / el
        self.changed.emit()
        self.jobFinished.emit(job)
        self._kick()


class QueueWidget(QWidget):
    """Таблица очереди: файл, голос, статус, прогресс, осталось."""
    COLS = ["Файл", "Голос", "Состояние", "Готово", "Осталось"]

    def __init__(self, queue: ExportQueue, parent=None):
        super().__init__(parent)
        self.q = queue
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.summary = QLabel("Здесь будет видно, как идёт запись книг в аудиофайлы.")
        v.addWidget(self.summary)
        self.t = QTableWidget(0, len(self.COLS))
        self.t.setHorizontalHeaderLabels(self.COLS)
        h = self.t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 4):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Fixed)
        self.t.setColumnWidth(3, 200)
        self.t.verticalHeader().setVisible(False)
        self.t.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.t.setContextMenuPolicy(Qt.CustomContextMenu)
        self.t.customContextMenuRequested.connect(self._menu)
        self.t.doubleClicked.connect(lambda idx: self._open_folder(self._job_at(idx.row())))
        v.addWidget(self.t, 1)
        row = QHBoxLayout()
        for text, fn in (("▲", lambda: self._move(-1)), ("▼", lambda: self._move(1)),
                         ("Остановить", self._cancel_sel), ("Начать заново", self._retry_sel),
                         ("Убрать из списка", self._remove_sel), ("Убрать готовые", self.q.clear_finished),
                         ("Остановить всё", self.q.cancel_all)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            if text in ("▲", "▼"):
                b.setFixedWidth(32)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.q.changed.connect(self.refresh)
        # «осталось» у идущего задания тикает каждую секунду
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._bars: dict[int, QProgressBar] = {}

    def _job_at(self, row):
        if 0 <= row < len(self.q.jobs):
            return self.q.jobs[row]
        return None

    def _selected(self):
        rows = sorted({i.row() for i in self.t.selectedIndexes()})
        return [self.q.jobs[r] for r in rows if r < len(self.q.jobs)]

    def _move(self, d):
        sel = self._selected()
        if len(sel) == 1:
            self.q.move(sel[0], d)
            self.t.selectRow(self.q.jobs.index(sel[0]))

    def _cancel_sel(self):
        for j in self._selected():
            self.q.cancel(j)

    def _retry_sel(self):
        for j in self._selected():
            self.q.retry(j)

    def _remove_sel(self):
        for j in self._selected():
            self.q.remove(j)

    def _open_folder(self, job):
        if job:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(job.out_path).parent)))

    def _menu(self, pos):
        job = self._job_at(self.t.rowAt(pos.y()))
        if not job:
            return
        m = QMenu(self)
        a_open = m.addAction("Открыть папку")
        a_play = m.addAction("Открыть файл") if job.files else None
        a_cancel = m.addAction("Отменить") if job.status in (PENDING, PREPARING, RUNNING) else None
        a_retry = m.addAction("Повторить") if job.status in (FAILED, CANCELLED) else None
        a_rm = m.addAction("Убрать из списка")
        act = m.exec(self.t.viewport().mapToGlobal(pos))
        if act == a_open:
            self._open_folder(job)
        elif a_play and act == a_play:
            QDesktopServices.openUrl(QUrl.fromLocalFile(job.files[0]))
        elif a_cancel and act == a_cancel:
            self.q.cancel(job)
        elif a_retry and act == a_retry:
            self.q.retry(job)
        elif act == a_rm:
            self.q.remove(job)

    def refresh(self):
        jobs = self.q.jobs
        sel = {id(j) for j in self._selected()}
        self.t.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            name = j.name() + (f"  (+{len(j.files) - 1} ч.)" if len(j.files) > 1 else "")
            items = [name, j.voice_name, j.status, None, ""]
            for c, val in enumerate(items):
                if c == 3:
                    continue
                it = self.t.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.t.setItem(r, c, it)
                it.setText(val)
            tip = j.out_path + (f"\nИсточник: {j.source}" if j.source else "")
            if j.error:
                tip += f"\nОшибка: {j.error}"
            self.t.item(r, 0).setToolTip(tip)
            self.t.item(r, 2).setToolTip(j.error or "")
            bar = self.t.cellWidget(r, 3)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                bar.setTextVisible(True)
                self.t.setCellWidget(r, 3, bar)
            bar.setMaximum(max(1, j.chars or j.total_paras or 1))
            if j.status == DONE:
                bar.setValue(bar.maximum())
                bar.setFormat(f"100%  •  {fmt_time(j.finished - j.started)}")
            elif j.status == RUNNING:
                bar.setValue(min(bar.maximum(), j.done_chars))
                bar.setFormat(f"%p%  •  {j.done_paras}/{j.total_paras} абз.")
            elif j.status == PREPARING:
                bar.setValue(0)
                bar.setFormat("извлечение текста…")
            else:
                bar.setValue(0)
                bar.setFormat(f"{j.total_paras} абз." if j.total_paras else "")
            if r in range(len(jobs)) and id(j) in sel:
                self.t.selectRow(r)
        self._tick()

    def _tick(self):
        now = time.time()
        for r, j in enumerate(self.q.jobs):
            it = self.t.item(r, 4)
            if it is None:
                continue
            if j.status == RUNNING:
                rem = j.eta
                if rem is not None and j.done_paras:
                    # уменьшаем между обновлениями прогресса
                    rem = max(0, rem - (now - getattr(j, "_eta_t", now)))
                txt = fmt_time(rem) if rem is not None else "считаю…"
            elif j.status == PENDING:
                rem = self.q.remaining(j)
                txt = ("≈ " + fmt_time(rem)) if rem is not None else "ждёт очереди"
            else:
                txt = ""
            it.setText(txt)
        # итог
        active = [j for j in self.q.jobs if j.status in (PENDING, PREPARING, RUNNING)]
        done = sum(1 for j in self.q.jobs if j.status == DONE)
        if not self.q.jobs:
            self.summary.setText("Здесь будет видно, как идёт запись книг в аудиофайлы.")
        elif active:
            tot = self.q.total_remaining()
            self.summary.setText(f"Книг в списке: {len(self.q.jobs)}, готово: {done}, ещё записывается: {len(active)}"
                                 + (f"   •   всё будет готово примерно через {fmt_time(tot)}" if tot is not None else ""))
        else:
            self.summary.setText(f"Запись закончена: готово {done} из {len(self.q.jobs)}. "
                                 f"Двойной щелчок по строке откроет папку с файлами.")

    def status_text(self) -> str:
        active = [j for j in self.q.jobs if j.status in (PENDING, PREPARING, RUNNING)]
        if not active:
            return ""
        run = next((j for j in self.q.jobs if j.status == RUNNING), None)
        tot = self.q.total_remaining()
        s = f"Запись в аудио: {len(active)}"
        if run and run.chars:
            s += f", текущее {int(run.done_chars * 100 / run.chars)}%"
        if tot is not None:
            s += f", осталось ≈ {fmt_time(tot)}"
        return s
