"""Токены доступны только через Windows Credential Manager, без plaintext fallback."""
import sys

SERVICE = 'WB Profit Desktop'


def backend():
    if sys.platform != 'win32':
        raise RuntimeError('Безопасное хранение токена реализовано только для Windows Credential Manager')
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def save_token(store: str, token: str):
    if not store.strip() or not token.strip():
        raise ValueError('Укажите магазин и токен')
    backend().set_password(SERVICE, store, token.strip())


def load_token(store: str) -> str | None:
    return backend().get_password(SERVICE, store)
