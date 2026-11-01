import unittest
from decimal import Decimal
import httpx
from wb_profit.api.transport import WbTransport, ApiError


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_rate_limit_still_persists_server_delay(self):
        blocked=[]
        client=WbTransport('https://statistics-api.wildberries.ru',lambda:'test-secret',interval=0,retries=0,
                           transport=httpx.MockTransport(lambda request:httpx.Response(429,headers={'Retry-After':'600'},text='test-secret')))
        client.rate_blocker=blocked.append
        try:
            with self.assertRaises(ApiError) as caught:await client.request('GET','/test')
            self.assertEqual(blocked,[600])
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.kind,'rate_limit')
            self.assertEqual(caught.exception.retry_after,600)
            self.assertNotIn('test-secret',str(caught.exception))
        finally:await client.close()

    async def test_transport_failure_is_retryable_but_json_and_auth_are_not(self):
        def offline(request):raise httpx.ConnectError('test-secret',request=request)
        for handler,retryable in [(offline,True),(lambda r:httpx.Response(401,text='test-secret'),False),
                                  (lambda r:httpx.Response(200,text='invalid'),False)]:
            client=WbTransport('https://statistics-api.wildberries.ru',lambda:'test-secret',interval=0,retries=0,
                               transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ApiError) as caught:await client.request('GET','/test')
                self.assertEqual(caught.exception.retryable,retryable)
                self.assertNotIn('test-secret',str(caught.exception))
            finally:await client.close()

    async def test_decimal_rate_limit_and_retry_after(self):
        now = [0.0]
        delays = []
        calls = []
        async def sleep(seconds):
            delays.append(seconds)
            now[0] += seconds
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={'Retry-After': '5'})
            return httpx.Response(200, content=b'{"amount": 123.456789}')
        client = WbTransport('https://statistics-api.wildberries.ru', lambda: 'test-secret',
            interval=2, transport=httpx.MockTransport(handler), clock=lambda: now[0], sleep=sleep)
        try:
            self.assertEqual((await client.request('GET', '/example'))['amount'], Decimal('123.456789'))
            self.assertEqual(delays, [5])
            self.assertEqual(calls[0].headers['Authorization'], 'test-secret')
        finally:
            await client.close()

    async def test_redirect_and_auth_error_do_not_leak_token(self):
        for status in [302, 401, 403, 400]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, headers={'Location': 'https://example.com'}, text='test-secret')
            client = WbTransport('https://statistics-api.wildberries.ru', lambda: 'test-secret',
                interval=0, transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ApiError) as caught:
                    await client.request('GET', '/example')
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertEqual(len(calls), 1)
            finally:
                await client.close()

    async def test_bad_json_and_missing_token(self):
        client = WbTransport('https://statistics-api.wildberries.ru', lambda: 'test-secret', interval=0,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<html>error</html>')))
        try:
            with self.assertRaises(ApiError):
                await client.request('GET', '/example')
            client.token = lambda: None
            with self.assertRaises(ApiError):
                await client.request('GET', '/example')
            with self.assertRaises(ValueError):
                await client.request('GET', '//example.com')
        finally:
            await client.close()

    async def test_unofficial_host_rejected(self):
        for host in ['http://statistics-api.wildberries.ru', 'https://wildberries.ru.evil.test',
                     'https://user@statistics-api.wildberries.ru', 'https://statistics-api.wildberries.ru/x']:
            with self.assertRaises(ValueError):
                WbTransport(host, lambda: 'test', interval=0)

    async def test_persistent_rate_reservation_and_wb_retry_header(self):
        now=[0.0]
        reserved=[5.0]
        calls=[]
        async def sleep(seconds):now[0]+=seconds
        def reserve(interval):
            if reserved[0]>now[0]:return reserved[0]-now[0]
            reserved[0]=now[0]+interval
            return 0
        def handler(request):
            calls.append(now[0])
            return httpx.Response(429,headers={'X-Ratelimit-Retry':'10'}) if len(calls)==1 else httpx.Response(200,json={})
        client=WbTransport('https://statistics-api.wildberries.ru',lambda:'test',interval=2,
                           transport=httpx.MockTransport(handler),sleep=sleep,clock=lambda:now[0])
        client.rate_reserver=reserve
        client.rate_blocker=lambda delay:reserved.__setitem__(0,max(reserved[0],now[0]+delay))
        try:
            await client.request('GET','/test')
            self.assertEqual(calls,[5.0,15.0])
        finally:await client.close()
