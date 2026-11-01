"""Защита запуска и восстановление в новый файл без изменения исходной базы."""
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from PySide6.QtCore import QLockFile
from .backups import BackupError, validate_backup, write_backup


class StartupError(ValueError):
    pass


class SessionLock:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(self.folder / '.wb-profit.lock'))
        # Возраст работающего экземпляра не должен снимать его блокировку.
        self.lock.setStaleLockTime(0)

    def acquire(self):
        if self.lock.tryLock(0):
            return True
        if self.lock.error() == QLockFile.LockError.LockFailedError:
            return False
        raise StartupError('Нет доступа к папке данных. Проверьте права доступа и свободное место.')

    def release(self):
        self.lock.unlock()


def active_database(folder):
    folder = Path(folder)
    pointer = folder / 'active-database.json'
    if not pointer.exists():
        return folder / 'wb-profit.sqlite'
    try:
        value = json.loads(pointer.read_text(encoding='utf-8'))
        filename = value['database']
        if not isinstance(filename, str) or not re.fullmatch(r'wb-profit-recovered-[0-9a-f]{32}\.sqlite', filename):
            raise ValueError('unsupported database name')
        path = folder / filename
        if not path.is_file():
            raise ValueError('database missing')
        return path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StartupError('Не удалось определить рабочую базу. Исходные файлы сохранены; выберите резервную копию.') from exc


def check_startup(folder):
    path = active_database(folder)
    if not path.exists():
        return path  # первый запуск; базу создаст Ledger
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise StartupError('База не прошла проверку целостности. Выберите резервную копию.')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'operations', 'settings', 'revisions'} <= tables:
                raise StartupError('Рабочая база имеет неподдерживаемый формат. Выберите резервную копию.')
        return path
    except sqlite3.Error as exc:
        raise StartupError('Не удалось прочитать рабочую базу. Исходные файлы сохранены; выберите резервную копию.') from exc


def recover_startup(folder, filename):
    """Вызывается под SessionLock. Оригинал и его WAL/SHM никогда не заменяются."""
    folder = Path(folder).resolve()
    source = Path(filename).resolve()
    if not source.is_file():
        raise BackupError('Файл резервной копии не найден.')
    temporary = None
    try:
        with tempfile.TemporaryDirectory(prefix='.wb-startup-', dir=folder) as directory:
            staged = Path(directory) / 'validated.sqlite'
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as incoming:
                write_backup(incoming, staged)
            with closing(sqlite3.connect(staged)) as snapshot:
                validate_backup(snapshot)
            from .sync import SyncStore
            cache = SyncStore(staged)
            cache.close()
            with closing(sqlite3.connect(staged)) as snapshot:
                validate_backup(snapshot)
                recovered = write_backup(snapshot, folder / f'wb-profit-recovered-{uuid.uuid4().hex}.sqlite')
        # Новый указатель появляется только после проверки и записи всей базы.
        handle, name = tempfile.mkstemp(prefix='.wb-active-', suffix='.json', dir=folder)
        temporary = Path(name)
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump({'database': recovered.name}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / 'active-database.json')
        return recovered
    except sqlite3.Error as exc:
        raise BackupError('Не удалось восстановить базу. Исходная база и выбранная копия не изменены.') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
