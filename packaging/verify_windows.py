"""Проверка установщика на одноразовом Windows runner, без обращения к WB API."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid


def gui_subsystem(executable):
    with Path(executable).open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Файл не является Windows EXE')
        stream.seek(60)
        offset = struct.unpack('<I', stream.read(4))[0]
        if offset > 1024 * 1024:
            raise ValueError('Некорректный заголовок Windows EXE')
        stream.seek(offset)
        if stream.read(4) != b'PE\0\0':
            raise ValueError('Не найден заголовок PE')
        stream.seek(offset + 24 + 68)
        return struct.unpack('<H', stream.read(2))[0]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(command, *, timeout=120, accepted=(0,)):
    process = subprocess.run(command, timeout=timeout, capture_output=True, text=True)
    if process.returncode not in accepted:
        raise RuntimeError(f'Проверка завершилась с кодом {process.returncode}: {Path(command[0]).name}')


def uninstall(executable, logs):
    run([str(executable), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LOG='+str(logs/'uninstall.log')])
    # Inno запускает отдельный помощник для удаления самого unins000.exe.
    # Возврат основного процесса ещё не означает, что помощник закрыл файл.
    deadline = time.monotonic() + 15
    while executable.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Деинсталлятор не завершил удаление собственного файла')
        time.sleep(0.05)


def verify(installer):
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Проверка установки предназначена только для одноразового Windows runner GitHub Actions')
    from PySide6.QtCore import QCoreApplication, QStandardPaths
    from wb_profit.storage import Ledger
    from wb_profit.sync import SyncStore
    from wb_profit.secrets import backend, SERVICE

    app = QCoreApplication.instance() or QCoreApplication([])
    app.setApplicationName('WB Profit Desktop')
    app.setOrganizationName('WB Profit Desktop')
    data_folder = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    desktop = Path(QStandardPaths.writableLocation(QStandardPaths.DesktopLocation))
    shortcut = desktop / 'WB Profit Desktop.lnk'
    if data_folder.exists() and any(data_folder.iterdir()) or shortcut.exists():
        raise RuntimeError('Обнаружены пользовательские данные или ярлык: тест не будет их изменять')
    created_folder = not data_folder.exists()
    data_folder.mkdir(parents=True, exist_ok=True)
    database = data_folder / 'wb-profit.sqlite'
    marker = uuid.uuid4().hex
    credential = 'ci-verification:' + uuid.uuid4().hex
    vault = backend()
    checks = []
    installer = Path(installer).resolve(strict=True)
    logs=installer.parent/'verification-logs'
    logs.mkdir(exist_ok=True)
    try:
        ledger = Ledger(database)
        ledger.set_setting('_app', 'release_verification', marker)
        cache = SyncStore(database)
        cache.close()
        ledger.close()
        database_hash = digest(database)
        vault.set_password(SERVICE, credential, marker)
        with tempfile.TemporaryDirectory(prefix='wb-windows-release-') as directory:
            install_folder = Path(directory) / 'application'
            executable = install_folder / 'WB Profit Desktop.exe'
            uninstaller = install_folder / 'unins000.exe'
            command = [str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                       '/DIR=' + str(install_folder), '/GROUP=WB Profit Desktop CI ' + marker]

            def preserved():
                if digest(database) != database_hash:
                    raise RuntimeError('Установка, запуск или удаление изменили пользовательскую базу')
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as saved:
                    row = saved.execute("SELECT payload FROM settings WHERE store='_app' AND name='release_verification'").fetchone()
                    if row is None or json.loads(row[0]) != marker:
                        raise RuntimeError('Потеря сохранённых настроек')
                if vault.get_password(SERVICE, credential) != marker:
                    raise RuntimeError('Потеря сохранённого секрета Windows Credential Manager')

            def installed():
                if gui_subsystem(executable) != 2:
                    raise RuntimeError('EXE собран с консольной подсистемой вместо Windows GUI')
                if not shortcut.is_file():
                    raise RuntimeError('Ярлык рабочего стола не создан')
                shortcut_check = subprocess.run(
                    ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                     '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:WB_VERIFY_SHORTCUT); Write-Output $s.TargetPath'],
                    env={**os.environ, 'WB_VERIFY_SHORTCUT': str(shortcut)},
                    check=True, capture_output=True, text=True, timeout=30)
                if Path(shortcut_check.stdout.strip()).resolve() != executable.resolve():
                    raise RuntimeError('Ярлык указывает на неверный EXE')
                run([str(executable), '--smoke-test'], timeout=30)
                preserved()

            uninstall_completed = False
            try:
                run([*command,'/LOG='+str(logs/'install.log')], accepted=(0, 3010))
                installed()
                checks.extend(['install', 'gui_subsystem', 'desktop_shortcut', 'installed_executable_start', 'credential_manager'])
                run([*command,'/LOG='+str(logs/'reinstall.log')], accepted=(0, 3010))
                installed()
                checks.append('reinstall_preserves_database_and_credentials')
                uninstall(uninstaller, logs)
                uninstall_completed = True
                if executable.exists() or shortcut.exists():
                    raise RuntimeError('Удаление оставило EXE или ярлык')
                preserved()
                checks.append('uninstall_preserves_database_and_credentials')
            finally:
                if not uninstall_completed and uninstaller.exists():
                    uninstall(uninstaller, logs)
        report = {'platform': sys.platform, 'runner_os': os.environ.get('RUNNER_OS'),
                  'checks': checks, 'installer': installer.name, 'sha256': digest(installer),
                  'limitations': ['No real WB API calls', 'Windows runner is not a Windows 11 user PC',
                                  'Reinstall tested with same version; cross-version upgrade not yet verified']}
        (installer.parent / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        (installer.parent / 'SHA256SUMS.txt').write_text(f'{report["sha256"]}  {installer.name}\n', encoding='utf-8')
        return report
    finally:
        if vault.get_password(SERVICE, credential) is not None:
            vault.delete_password(SERVICE, credential)
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(database) + suffix).unlink(missing_ok=True)
        if created_folder:
            data_folder.rmdir()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installer', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.installer), indent=2))
