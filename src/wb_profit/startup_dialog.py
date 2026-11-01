"""Короткий экран проверки базы и восстановления без консоли."""
from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QFileDialog
from .startup import StartupError, check_startup, recover_startup
from .backups import BackupError


class StartupWorker(QThread):
    def __init__(self, folder, filename=None, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.filename = filename
        self.database = None
        self.error_message = None

    def run(self):
        try:
            self.database = (recover_startup(self.folder, self.filename) if self.filename is not None
                             else check_startup(self.folder))
        except (StartupError, BackupError) as exc:
            self.error_message = str(exc)
        except OSError:
            self.error_message = 'Нет доступа к файлу или недостаточно места на диске. Исходные файлы не удаляйте.'
        except Exception:
            self.error_message = 'Не удалось открыть базу. Исходные файлы не удаляйте; выберите другую резервную копию.'


class StartupDialog(QDialog):
    def __init__(self, folder, failure=None, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.database = None
        self.worker = None
        self.abandoned = False
        self.setWindowTitle('WB Profit Desktop — запуск')
        self.resize(520, 230)
        layout = QVBoxLayout(self)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        notice = QLabel('При восстановлении копия станет новой рабочей базой. Исходная база и файлы журнала останутся на месте. Токены Windows Credential Manager не изменятся.')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.choose = QPushButton('Восстановить из резервной копии')
        self.choose.clicked.connect(self.choose_backup)
        layout.addWidget(self.choose)
        self.cancel = QPushButton('Закрыть')
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)
        if failure:
            self.message.setText(failure)
        else:
            self.choose.setEnabled(False)
            self.message.setText('Проверка локальной базы…')
            QTimer.singleShot(0, self.start)

    def choose_backup(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Резервная копия', str(self.folder), 'SQLite (*.sqlite *.db);;Все файлы (*)')
        if filename:
            self.start(filename)

    def start(self, filename=None):
        if self.worker is not None or self.abandoned:
            return
        self.choose.setEnabled(False)
        self.cancel.setEnabled(False)
        self.message.setText('Проверка и восстановление копии…' if filename else 'Проверка локальной базы…')
        self.worker = StartupWorker(self.folder, filename, self)
        self.worker.finished.connect(self.finished_work)
        self.worker.start()

    def finished_work(self):
        worker = self.worker
        self.worker = None
        self.choose.setEnabled(True)
        self.cancel.setEnabled(True)
        if worker.error_message:
            self.message.setText(worker.error_message)
        else:
            self.database = worker.database
            self.accept()
        worker.deleteLater()

    def reject(self):
        if self.worker is None:
            self.abandoned=True
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
