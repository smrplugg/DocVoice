"""Окно «Связь с разработчиком»: комментарий, контакт и журнал — одной кнопкой."""
from __future__ import annotations

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from .. import support
from ..log import log, make_report
from .common import FuncThread


class ContactDialog(QDialog):
    def __init__(self, parent=None, problem: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Связь с разработчиком")
        self.resize(720, 620)
        self.thread: FuncThread | None = None
        v = QVBoxLayout(self)

        v.addWidget(QLabel("<b>Что случилось или что вы хотите сказать?</b>"))
        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("Например: «Нажимаю „Читать“, а звука нет» или «Хочу, чтобы "
                                        "программа запоминала, где я остановился».")
        if problem:
            self.comment.setPlainText(f"Появилась ошибка:\n{problem}\n\nЧто я делал перед этим: ")
        self.comment.setMinimumHeight(140)
        v.addWidget(self.comment)

        v.addWidget(QLabel("Как с вами связаться, чтобы ответить (почта, телефон или Telegram) — по желанию:"))
        self.contact = QLineEdit()
        self.contact.setPlaceholderText("например, ivan@mail.ru")
        v.addWidget(self.contact)

        self.attach = QCheckBox("Приложить журнал работы программы (очень помогает найти причину)")
        self.attach.setChecked(True)
        v.addWidget(self.attach)
        note = QLabel("В журнале — что делала программа и какие были ошибки, сведения о компьютере "
                      "и настройки. Названий ваших книг, их текста и путей к файлам там нет. "
                      "Последние строки журнала:")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        self.tail = QPlainTextEdit(support.log_tail(120) or "(журнал пока пуст)")
        self.tail.setReadOnly(True)
        self.tail.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = self.tail.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.tail.setFont(f)
        self.tail.moveCursor(QTextCursor.MoveOperation.End)
        self.tail.moveCursor(QTextCursor.MoveOperation.StartOfLine)   # конец журнала, но без прокрутки вбок
        self.attach.toggled.connect(self.tail.setEnabled)
        v.addWidget(self.tail, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setMinimumHeight(38)
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self._send)
        cancel = QPushButton("Закрыть")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.send_btn)
        row.addWidget(cancel)
        v.addLayout(row)
        self.comment.moveCursor(QTextCursor.MoveOperation.End)
        self.comment.setFocus()

    def _send(self):
        text = self.comment.toPlainText().strip()
        if not text:
            QMessageBox.information(self, self.windowTitle(), "Напишите, пожалуйста, пару слов о том, что случилось.")
            return
        log.info("Пользователь отправляет сообщение разработчику")
        if not support.configured():
            self._fallback("")
            return
        self.send_btn.setEnabled(False)
        self.status.setText("Отправляю… Это может занять до минуты.")
        th = FuncThread(lambda progress, stop: support.send(text, self.contact.text().strip(),
                                                            self.attach.isChecked()),
                        parent=self, name="отправка сообщения разработчику")
        th.done.connect(lambda _: self._sent())
        th.failed.connect(self._fallback)
        self.thread = th
        th.start()

    def _sent(self):
        self.status.setText("")
        QMessageBox.information(self, self.windowTitle(), "Сообщение отправлено. Спасибо!")
        self.accept()

    def _fallback(self, err: str):
        """Без интернета или без настроенного сервера: архив на рабочий стол + письмо в почтовой программе."""
        self.send_btn.setEnabled(True)
        self.status.setText("")
        report = None
        if self.attach.isChecked():
            try:
                desk = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
                report = make_report(__import__("pathlib").Path(desk) if desk else None)
            except Exception:
                log.exception("Не удалось сохранить отчёт для письма")
        why = f"Не получилось отправить сообщение напрямую ({err}).\n\n" if err else ""
        QMessageBox.information(
            self, self.windowTitle(),
            why + "Сейчас откроется почтовая программа с готовым письмом разработчику "
                  f"({support.DEVELOPER_EMAIL}).\n\n"
            + (f"Приложите к письму файл с рабочего стола:\n{report.name}\n\n" if report else "")
            + "Если почтовая программа не открылась — напишите на этот адрес сами "
              "с почты в браузере и приложите этот файл.")
        QDesktopServices.openUrl(QUrl(support.mailto_url(self.comment.toPlainText().strip(),
                                                         self.contact.text().strip())))
        if report:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(report.parent)))
        self.accept()

    def reject(self):
        if self.thread and self.thread.isRunning():
            self.thread.wait(1000)
        super().reject()
