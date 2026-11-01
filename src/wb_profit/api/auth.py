"""Локальное чтение заявленных прав JWT. Подлинность подтверждает только сервер WB."""
import base64
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass

CATEGORIES = {'content': (1, 'Контент'), 'analytics': (2, 'Аналитика'),
              'statistics': (5, 'Статистика'), 'promotion': (6, 'Продвижение'), 'finance': (13, 'Финансы')}

@dataclass(frozen=True)
class Claims:
    mask: int
    account_type: int
    expired: bool
    sandbox: bool
    expires_at: int

    def permits(self, category):
        return bool(self.mask & (1 << CATEGORIES[category][0]))

    @property
    def fast_limits(self):
        return self.account_type in (3, 4)


def inspect_token(token: str) -> Claims:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        mask, acc, exp = payload['s'], payload['acc'], payload['exp']
        if any(isinstance(v, bool) or not isinstance(v, int) for v in [mask, acc, exp]) or mask < 0:
            raise ValueError()
        datetime.fromtimestamp(exp, timezone.utc)
        return Claims(mask, acc, exp <= time.time(), bool(payload.get('t', False)), exp)
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError, OSError):
        raise ValueError('Не удалось прочитать свойства токена WB. Значение токена не сохраняется в журнале.') from None
