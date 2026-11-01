"""Проверки PE-заголовка и настоящего Windows Credential Manager."""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import uuid

SPEC = importlib.util.spec_from_file_location('windows_verify', Path(__file__).parents[1] / 'packaging' / 'verify_windows.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class ReleaseTests(unittest.TestCase):
    def test_gui_executable_is_distinguished_from_console_and_broken_files(self):
        # Синтетические PE-заголовки используются исключительно в этом тесте.
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'sample.exe'
            for subsystem in (2, 3):
                header = bytearray(256)
                header[:2] = b'MZ'
                struct.pack_into('<I', header, 60, 128)
                header[128:132] = b'PE\0\0'
                struct.pack_into('<H', header, 128 + 24 + 68, subsystem)
                exe.write_bytes(header)
                self.assertEqual(VERIFY.gui_subsystem(exe), subsystem)
            for payload in (b'invalid', b'MZ' + b'\0' * 254):
                exe.write_bytes(payload)
                with self.assertRaises(ValueError):
                    VERIFY.gui_subsystem(exe)

    def test_installer_lifecycle_cannot_run_outside_ephemeral_windows_ci(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'одноразового Windows'):
                VERIFY.verify('missing.exe')


@unittest.skipUnless(sys.platform == 'win32', 'Требуется настоящий Windows Credential Manager')
class WindowsVaultTests(unittest.TestCase):
    def test_secret_roundtrip_and_backend_reopen_without_plaintext_storage(self):
        from wb_profit.secrets import backend, save_token, load_token, SERVICE
        identifier = 'ci-test:' + uuid.uuid4().hex
        value = uuid.uuid4().hex  # локальный тестовый секрет, не токен WB
        vault = backend()
        try:
            self.assertIsNone(load_token(identifier))
            save_token(identifier, value)
            self.assertEqual(load_token(identifier), value)
            self.assertEqual(backend().get_password(SERVICE, identifier), value)
        finally:
            if vault.get_password(SERVICE, identifier) is not None:
                vault.delete_password(SERVICE, identifier)
        self.assertIsNone(load_token(identifier))
