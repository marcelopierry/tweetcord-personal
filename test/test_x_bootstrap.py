import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from src.notification.x_bootstrap import prepare_x_bootstrap


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def app(self, cookies):
        return NS(request=NS(cookies=cookies, _init_local_api=AsyncMock(),
            _transaction=None, _guest_token=None, get_home_html=AsyncMock(return_value='page'),
            _get_guest_token=AsyncMock(return_value='guest')))

    async def test_authenticated_bootstrap_keeps_cookies_and_caches(self):
        app = self.app({'auth_token':'test-token'})
        original = app.request._init_local_api
        prepare_x_bootstrap(app)
        with patch('src.notification.x_bootstrap.TransactionGenerator', return_value='transaction') as generator:
            await app.request._init_local_api()
            await app.request._init_local_api()
            generator.assert_called_once_with('page')
        original.assert_not_awaited()
        self.assertEqual(app.request.cookies, {'auth_token':'test-token'})
        app.request._get_guest_token.assert_awaited_once()

    async def test_failure_does_not_strip_authentication(self):
        app = self.app({'auth_token':'test-token'})
        prepare_x_bootstrap(app)
        app.request.get_home_html.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await app.request._init_local_api()
        self.assertEqual(app.request.cookies, {'auth_token':'test-token'})
        self.assertIsNone(app.request._transaction)

    async def test_guest_behavior_unchanged(self):
        app = self.app({})
        original = app.request._init_local_api
        prepare_x_bootstrap(app)
        await app.request._init_local_api()
        original.assert_awaited_once()
        app.request.get_home_html.assert_not_awaited()
