"""Асинхронный HTTP с безопасной обработкой ошибок и сохранением Decimal."""
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from typing import Callable, Awaitable

import httpx


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, *, retryable=None, retry_after=None, kind=None):
        super().__init__(message)
        self.status = status
        self.retryable = status in {408,429,500,502,503,504} if retryable is None else bool(retryable)
        self.retry_after = retry_after
        self.kind = kind or ('rate_limit' if status == 429 else 'service' if self.retryable else 'permanent')


class WbTransport:
    def __init__(self, host: str, token: Callable[[], str | None], *, interval: float,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, retries: int = 3):
        parsed = urlsplit(host)
        if (parsed.scheme != 'https' or not parsed.hostname or
                not parsed.hostname.endswith('.wildberries.ru') or parsed.username or
                parsed.password or parsed.port not in (None, 443) or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Разрешён только HTTPS-хост официального API Wildberries')
        if interval < 0 or retries < 0:
            raise ValueError('Неверные параметры повторов и ограничения частоты')
        self.host = f'https://{parsed.hostname}'
        self.token = token
        self.interval = interval
        self.sleep = sleep
        self.clock = clock
        self.retries = retries
        self.next_request = 0.0
        self.rate_reserver = None
        self.rate_blocker = None
        self.lock = asyncio.Lock()
        # Системное CA-хранилище сохраняет доверие прокси среды и штатные корни ОС.
        import ssl
        self.client = httpx.AsyncClient(transport=transport, timeout=60,
                                       verify=ssl.create_default_context(), follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    def retry_delay(self, header: str | None, attempt: int) -> float:
        if header:
            try:
                value = float(header)
                if value >= 0 and value < float('inf'):
                    return value
            except ValueError:
                try:
                    stamp = parsedate_to_datetime(header)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    return max(0, (stamp - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return min(60, 2 ** attempt) + random.random()

    async def request(self, method: str, path: str, *, params=None, body=None):
        # Нельзя отправить токен через absolute URL, //host или редирект.
        if not path.startswith('/') or path.startswith('//') or '\\' in path or urlsplit(path).scheme:
            raise ValueError('Требуется относительный путь API')
        method = method.upper()
        if method not in {'GET', 'POST'}:
            raise ValueError('Транспорт аналитики допускает только GET и POST')
        token = self.token()
        if not token:
            raise ApiError('Токен магазина не сохранён')
        async with self.lock:
            for attempt in range(self.retries + 1):
                delay = self.next_request - self.clock()
                if delay > 0:
                    await self.sleep(delay)
                if self.rate_reserver is not None:
                    while True:
                        persisted_delay = self.rate_reserver(self.interval)
                        if persisted_delay <= 0:
                            break
                        await self.sleep(persisted_delay)
                self.next_request = self.clock() + self.interval
                try:
                    response = await self.client.request(method, self.host + path,
                        params=params, json=body, headers={'Authorization': token})
                except httpx.TransportError:
                    if attempt == self.retries:
                        raise ApiError('Сеть недоступна или не удалось установить защищённое соединение', retryable=True, kind='network') from None
                    await self.sleep(self.retry_delay(None, attempt))
                    continue
                status = response.status_code
                if status in {401, 403}:
                    raise ApiError('Токен недействителен либо не хватает разрешений для источника', status)
                if status == 429 or status in {408, 500, 502, 503, 504}:
                    delay = self.retry_delay(response.headers.get('Retry-After') or response.headers.get('X-Ratelimit-Retry'), attempt)
                    # Не делаем запрос до указанного WB времени даже при отмене текущей задачи.
                    self.next_request = max(self.next_request, self.clock() + delay)
                    if self.rate_blocker is not None:
                        self.rate_blocker(delay)
                    if attempt == self.retries:
                        raise ApiError('Лимит запросов или временная ошибка Wildberries', status, retry_after=delay)
                    continue
                if status == 204:
                    return None
                if not 200 <= status < 300:
                    raise ApiError(f'Wildberries вернул ошибку HTTP {status}', status)
                try:
                    return json.loads(response.content, parse_float=Decimal,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Неконечное число')))
                except (ValueError, UnicodeError):
                    raise ApiError('Ответ Wildberries не соответствует формату JSON', status) from None
