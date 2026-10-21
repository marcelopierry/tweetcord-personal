import importlib
import os
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
import aiosqlite
import discord

from src.notification.notifier_commands import XClientNotReady, notifier_x_client
from src.notification.x_connection import XConnection
from src.sync_db.sync_db import sync_db


class NotifierClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_methods_use_the_existing_transport(self):
        client = NS(me=NS(id='1'), get_user_info=AsyncMock(return_value='profile'),
                    unfollow_user=AsyncMock(return_value=True),
                    disable_user_notification=AsyncMock(return_value=True))
        factory = AsyncMock()
        connection = XConnection(client, factory)
        self.assertEqual(await connection.get_user_info('example'), 'profile')
        self.assertTrue(await connection.unfollow_user('100'))
        self.assertTrue(await connection.disable_user_notification('100'))
        client.get_user_info.assert_awaited_once_with('example')
        client.unfollow_user.assert_awaited_once_with('100')
        client.disable_user_notification.assert_awaited_once_with('100')
        factory.assert_not_awaited()

    async def test_sync_borrows_client_without_reauthenticating_or_closing_it(self):
        client = NS(follow_user=AsyncMock(), enable_user_notification=AsyncMock(),
                    request=NS(session=NS(aclose=AsyncMock())))
        with patch('src.sync_db.sync_db.authenticate_x', new=AsyncMock()) as authenticate, \
             patch('src.sync_db.sync_db.asyncio.sleep', new=AsyncMock()):
            await sync_db({'100':'Account1'}, {'Account1':client})
        authenticate.assert_not_awaited()
        client.follow_user.assert_awaited_once_with('100')
        client.request.session.aclose.assert_not_awaited()


class CrossServerNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {'DATA_PATH':self.directory.name,
                                                   'TWITTER_TOKEN':'Account1:test-token'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from src.i18n import init_i18n
        init_i18n('en')
        module = importlib.import_module('cogs.notification')
        self.Notification = module.Notification
        from src.db_function.init_db import init_db
        await init_db()
        self.db_path = os.path.join(self.directory.name, 'tracked_accounts.db')
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO user VALUES ('100','example','2026-10-21','Account1',1)")
            await db.execute("INSERT INTO channel VALUES ('111','11')")
            await db.execute("INSERT INTO notification (user_id,channel_id,role_id) VALUES ('100','111','')")
            await db.commit()
        self.client = NS(unfollow_user=AsyncMock(), disable_user_notification=AsyncMock())
        self.cog = self.Notification.__new__(self.Notification)
        self.cog.account_tracker = NS(x_clients={'Account1':self.client}, addTask=AsyncMock(), removeTask=AsyncMock())
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id, self.channel.mention = 222, '<#222>'
        self.channel.guild = NS(id=22, name='Second server')
        self.interaction = NS(channel=self.channel, channel_id=222, guild=self.channel.guild,
                              guild_id=22, data={}, response=NS(defer=AsyncMock()),
                              followup=NS(send=AsyncMock()))

    async def add(self):
        with patch('cogs.notification.resolve_user', new=AsyncMock(return_value=NS(id='100',username='example'))) as resolve, \
             patch('cogs.notification.ensure_subscription', new=AsyncMock(return_value=False)) as subscribe, \
             patch('src.notification.x_auth.authenticate_x', new=AsyncMock(side_effect=AssertionError('must not reauthenticate'))):
            await self.Notification.notifier.callback(self.cog, self.interaction,
                username='example', account_used='Account1')
        return resolve, subscribe

    async def test_add_existing_account_to_second_server_uses_shared_connection(self):
        resolve, subscribe = await self.add()
        resolve.assert_awaited_once_with(self.client, 'example')
        subscribe.assert_awaited_once_with(self.client, '100')
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute('SELECT channel_id,enabled FROM notification ORDER BY channel_id')).fetchall()
            server = await (await db.execute("SELECT server_id FROM channel WHERE id='222'")).fetchone()
        self.assertEqual(rows, [('111',1), ('222',1)])
        self.assertEqual(server, ('22',))
        self.cog.account_tracker.addTask.assert_not_awaited()
        self.assertNotIn('failed', self.interaction.followup.send.await_args.args[0].lower())

    async def test_reconnecting_account_gets_clear_reply_and_no_notifier_write(self):
        self.cog.account_tracker.x_clients.clear()
        resolve, subscribe = await self.add()
        resolve.assert_not_awaited()
        subscribe.assert_not_awaited()
        self.assertIn('reconnecting', self.interaction.followup.send.await_args.args[0])
        async with aiosqlite.connect(self.db_path) as db:
            self.assertEqual((await (await db.execute('SELECT count(*) FROM notification')).fetchone())[0], 1)

    async def test_removing_second_server_does_not_unfollow_first_servers_account(self):
        await self.add()
        await self.Notification.r_notifier.callback(self.cog, self.interaction, username='example')
        self.client.unfollow_user.assert_not_awaited()
        self.client.disable_user_notification.assert_not_awaited()
        async with aiosqlite.connect(self.db_path) as db:
            self.assertEqual(await (await db.execute('SELECT channel_id,enabled FROM notification ORDER BY channel_id')).fetchall(),
                             [('111',1), ('222',0)])
