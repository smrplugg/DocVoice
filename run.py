"""DocVoice — озвучка документов.

Запуск: python run.py [файл]
Самопроверка без окна (для поддержки): python run.py --selftest  (DocVoice.exe --selftest)
"""
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def selftest() -> int:
    """Проверка без окна: результат — в журнале и в data/logs/selftest.txt."""
    from docvoice import log
    from docvoice.selftest import run_checks, write_report
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841 (нужен для проверки звука/шрифтов)
    results = run_checks()
    path = write_report(results)
    log.log.info(f"Самопроверка без окна: {path}")
    return 0 if all(r.ok or r.optional for r in results) else 1


def main():
    from docvoice import log
    log.setup()
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    from PySide6.QtWidgets import QApplication
    from docvoice.gui.main_window import MainWindow

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DocVoice.App")
        except Exception:
            pass
    app = QApplication(sys.argv)
    log.install_qt_handler()
    app.setApplicationName("DocVoice")
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    if files and os.path.exists(files[0]):
        w.open_file(files[0])
    code = app.exec()
    log.log.info(f"Программа закрыта (код {code})")
    sys.exit(code)


if __name__ == "__main__":
    main()
