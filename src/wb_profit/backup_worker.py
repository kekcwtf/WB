"""Дисковые операции вне GUI-потока; у потока своё SQLite-соединение."""
from PySide6.QtCore import QThread
from threading import Event
from .backups import BackupError
from .storage import Ledger


class BackupWorker(QThread):
    def __init__(self, database, filename, *, restore=False, parent=None):
        super().__init__(parent)
        self.database = database
        self.filename = filename
        self.restore = restore
        self.result = None
        self.error_message = None

    def run(self):
        ledger = None
        try:
            ledger = Ledger(self.database)
            self.result = ledger.restore(self.filename) if self.restore else ledger.backup(self.filename)
        except BackupError as exc:
            self.error_message = str(exc)
        except OSError:
            self.error_message = 'Нет доступа к файлу или недостаточно места на диске. Рабочую базу и копии не удаляйте.'
        except Exception:
            self.error_message = 'Операция с базой не завершена. Рабочую базу и резервные копии не удаляйте.'
        finally:
            if ledger is not None:
                ledger.close()


class AutoBackupWorker(QThread):
    def __init__(self,database,parent=None):
        super().__init__(parent)
        self.database=database
        self.result=None;self.error_message=None;self.cancelled=False
        self.stop_requested=Event()

    def cancel(self):self.stop_requested.set()

    def check_cancel(self):
        if self.stop_requested.is_set():raise InterruptedError('Автокопия отменена')

    def run(self):
        from .auto_backups import automatic_backup
        ledger=None
        try:
            self.check_cancel()
            ledger=Ledger(self.database)
            self.result=automatic_backup(ledger,self.database,cancel_check=self.check_cancel)
        except InterruptedError:self.cancelled=True
        except BackupError as exc:self.error_message=str(exc)
        except OSError:self.error_message='Автокопия не создана: проверьте доступ к папке и свободное место.'
        except Exception:self.error_message='Не удалось создать автокопию. Существующие копии и рабочую базу не удаляйте.'
        finally:
            if ledger is not None:ledger.close()
