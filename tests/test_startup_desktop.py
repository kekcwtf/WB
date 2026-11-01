import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import time
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog
from wb_profit.startup_dialog import StartupDialog
from wb_profit.startup import active_database
from wb_profit.desktop import Window
from wb_profit.storage import Ledger


class StartupDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, dialog):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.processEvents()
            if dialog.worker is None:
                return
            # Уступаем GIL Python-потоку проверки SQLite, сохраняя обработку Qt.
            time.sleep(0.005)
        self.fail('Проверка базы не завершилась')

    def test_corrupted_database_can_be_recovered_before_main_window_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'wb-profit.sqlite').write_bytes(b'corrupted')
            source = root / 'backup.sqlite'
            ledger = Ledger(source)
            ledger.set_setting('_app', 'stores', [{'id': 'shop', 'name': 'Восстановленный магазин'}])
            ledger.close()
            dialog = StartupDialog(root)
            dialog.show()
            self.app.processEvents()
            self.wait(dialog)
            self.assertIsNone(dialog.database)
            self.assertTrue(dialog.choose.isEnabled())
            self.assertIn('Исходные файлы сохранены', dialog.message.text())
            ticks=[]
            QTimer.singleShot(0, lambda: ticks.append(True))
            dialog.start(source)
            self.wait(dialog)
            self.assertTrue(ticks)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            window = Window(dialog.database)
            self.assertEqual(window.current_store, 'shop')
            self.assertEqual(window.store.currentText(), 'Восстановленный магазин')
            window.close()
            self.assertEqual(active_database(root), dialog.database)
            self.assertEqual((root / 'wb-profit.sqlite').read_bytes(), b'corrupted')
            dialog.close()

    def test_cancelled_startup_does_not_launch_queued_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = StartupDialog(Path(folder))
            dialog.reject()
            self.app.processEvents()
            self.assertIsNone(dialog.worker)
            self.assertFalse((Path(folder) / 'wb-profit.sqlite').exists())
            dialog.close()

    def test_partial_window_failure_closes_its_database_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            captured=[]
            real_init=Ledger.__init__

            def remember(ledger, filename):
                real_init(ledger, filename)
                captured.append(ledger)

            def fail_settings(_window):
                raise ValueError('invalid saved settings')

            with patch.object(Ledger, '__init__', remember), patch.object(Window, 'build_settings', fail_settings):
                with self.assertRaises(ValueError):
                    Window(path)
            import sqlite3
            with self.assertRaises(sqlite3.ProgrammingError):
                captured[0].db.execute('SELECT 1')
            self.app.processEvents()
            path.unlink()  # закрытый файл можно удалить и на Windows

    def test_complete_launch_recovers_and_starts_main_window_in_separate_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            (root/'wb-profit.sqlite').write_bytes(b'corrupted original')
            source=root/'backup.sqlite'
            ledger=Ledger(source)
            ledger.set_setting('_app','stores',[{'id':'shop','name':'Магазин'}])
            ledger.close()
            script='''
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from wb_profit.desktop import run_desktop
from wb_profit.startup_dialog import StartupDialog
from wb_profit.startup import SessionLock,active_database
app=QApplication([])
root=Path(sys.argv[1]);source=Path(sys.argv[2]);chosen=[]
def select_backup():
    for widget in app.topLevelWidgets():
        if isinstance(widget,StartupDialog) and widget.isVisible() and widget.worker is None and not chosen:
            chosen.append(True);widget.start(source)
timer=QTimer();timer.timeout.connect(select_backup);timer.start(10)
assert run_desktop(app,root,smoke=True)==0
assert chosen
assert active_database(root).name.startswith('wb-profit-recovered-')
assert (root/'wb-profit.sqlite').read_bytes()==b'corrupted original'
lock=SessionLock(root);assert lock.acquire();lock.release()
'''
            process=subprocess.run([sys.executable,'-c',script,str(root),str(source)],
                                   capture_output=True,text=True,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
