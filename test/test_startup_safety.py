import ast
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
import discord

from src.discord_guard import SessionStartGuard, ProtectedBot
from src.notification.x_auth import authenticate_x, recover_x_account


class StartupSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_identify_limit_survives_restarts_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sessions.db')
            first = SessionStartGuard(path, limit=2, window=1000, spacing=60)
            self.assertEqual(await first.reserve(100), 0)
            self.assertEqual(await first.reserve(110), 50)
            self.assertEqual(await first.reserve(160), 0)
            second = SessionStartGuard(path, limit=2, window=1000, spacing=60)
            self.assertEqual(await second.reserve(300), 800)
            self.assertEqual(await second.reserve(1100), 0)

    async def test_concurrent_processes_share_identify_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sessions.db')
            guards = [SessionStartGuard(path, limit=2, window=1000, spacing=0) for _ in range(6)]
            waits = await asyncio.gather(*(guard.reserve(100) for guard in guards))
            self.assertEqual(waits.count(0), 2)
            self.assertEqual(waits.count(1000), 4)

    async def test_one_time_initialization_does_not_repeat_on_reconnect(self):
        bot = NS(initialize_once=AsyncMock())
        await ProtectedBot.setup_hook(bot)
        await ProtectedBot.setup_hook(bot)
        bot.initialize_once.assert_awaited_once()

    def app(self, token='configured'):
        return NS(session=NS(logged_in=True, cookies_dict=lambda: {'auth_token':token,'ct0':'csrf'}),
                  request=NS(session=NS(aclose=AsyncMock())),
                  connect=AsyncMock(return_value=object()), load_auth_token=AsyncMock())

    async def test_saved_session_avoids_business_login_page(self):
        app = self.app()
        with patch('src.notification.x_auth.Twitter', return_value=app), \
             patch('src.notification.x_auth.prepare_x_bootstrap', side_effect=lambda app: app):
            self.assertIs(await authenticate_x('Account1', 'configured'), app)
        app.connect.assert_awaited_once()
        app.load_auth_token.assert_not_awaited()

    async def test_changed_token_does_not_reuse_other_saved_identity(self):
        app = self.app('old-token')
        with patch('src.notification.x_auth.Twitter', return_value=app), \
             patch('src.notification.x_auth.prepare_x_bootstrap', side_effect=lambda app: app):
            await authenticate_x('Account1', 'configured')
        app.connect.assert_not_awaited()
        app.load_auth_token.assert_awaited_once_with('configured')

    async def test_upstream_timeout_closes_failed_client_without_fresh_login(self):
        app = self.app()
        app.connect.side_effect = TimeoutError
        with patch('src.notification.x_auth.Twitter', return_value=app), \
             patch('src.notification.x_auth.prepare_x_bootstrap', side_effect=lambda app: app):
            with self.assertRaises(TimeoutError):
                await authenticate_x('Account1', 'configured')
        app.request.session.aclose.assert_awaited_once()
        app.load_auth_token.assert_not_awaited()

    async def test_x_failure_recovers_in_process_with_bounded_backoff(self):
        client = object()
        authentication = AsyncMock(side_effect=[TimeoutError]*7+[client])
        with patch('src.notification.x_auth.authenticate_x', authentication), \
             patch('src.notification.x_auth.asyncio.sleep', new=AsyncMock()) as sleep:
            self.assertIs(await recover_x_account('Account1', 'configured'), client)
        self.assertEqual([call.args[0] for call in sleep.await_args_list],
                         [60, 120, 240, 480, 960, 1800, 1800])

    async def test_cancelled_x_recovery_shuts_down_normally(self):
        with patch('src.notification.x_auth.authenticate_x', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await recover_x_account('Account1', 'configured')

    def test_invalid_discord_token_exits_without_restart(self):
        # Execute only the entry point; no real Discord requests or credentials.
        main = next(node for node in ast.parse(Path('bot.py').read_text()).body
                    if isinstance(node, ast.If) and '__name__' in ast.unparse(node.test))
        bot = NS(run=Mock(side_effect=discord.LoginFailure('invalid')))
        namespace = {'__name__':'__main__', 'discord':discord, 'bot':bot,
                     'os':NS(getenv=lambda key: 'test-token'), 'sys':__import__('sys'),
                     'log':NS(critical=Mock())}
        with self.assertRaises(SystemExit) as result:
            exec(compile(ast.Module(body=[main], type_ignores=[]), 'entry', 'exec'), namespace)
        self.assertEqual(result.exception.code, 78)
