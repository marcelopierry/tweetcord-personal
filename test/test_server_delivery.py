import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord

from src.notification.account_tracker import AccountTracker
from src.notification.server_delay import ServerDelays
from src.notification.server_delivery import ServerDelivery


class ServerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name+'/test.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE runtime_setting(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                             'CREATE TABLE notification(account TEXT,settings TEXT);'
                             'INSERT INTO notification VALUES("example","all media,retweets,quotes");'
                             'INSERT INTO runtime_setting VALUES("notification_delay_seconds:11","180");')
        self.state = ServerDelivery()
        self.now = datetime.now(timezone.utc)
        self.before = NS(id='100', created_on=self.now-timedelta(seconds=10))
        self.after = NS(id='200', created_on=self.now+timedelta(seconds=10))
        self.tracker = AccountTracker.__new__(AccountTracker)
        self.tracker.db_path = self.path
        self.tracker.pending_tweets = {}
        self.tracker.ready_tweets = {}
        self.tracker.server_delays = ServerDelays(180)
        self.tracker.server_delivery = self.state
        self.tracker.server_command_locks = {}

    async def test_enabled_by_default_and_pause_is_server_specific(self):
        self.assertTrue(self.state.enabled(11))
        await self.state.set_enabled(self.path, 11, False)
        self.assertFalse(self.state.allows(11, self.after))
        self.assertTrue(self.state.allows(22, self.before))

    async def test_pause_and_resume_cutoff_survive_restart(self):
        await self.state.set_enabled(self.path, 11, False)
        restarted = ServerDelivery()
        await restarted.load(self.path)
        self.assertFalse(restarted.enabled(11))
        await restarted.set_enabled(self.path, 11, True, self.now)
        restarted = ServerDelivery()
        await restarted.load(self.path)
        self.assertTrue(restarted.enabled(11))
        self.assertFalse(restarted.allows(11, self.before))
        self.assertTrue(restarted.allows(11, self.after))

    async def test_repeated_enable_does_not_drop_new_pending_posts(self):
        self.assertFalse(await self.state.set_enabled(self.path, 11, True))
        await self.state.set_enabled(self.path, 11, False)
        await self.state.set_enabled(self.path, 11, True, self.now)
        self.assertFalse(await self.state.set_enabled(self.path, 11, True, self.now+timedelta(hours=1)))
        self.assertTrue(self.state.allows(11, self.after))

    async def test_settings_and_other_server_are_not_mutated(self):
        with sqlite3.connect(self.path) as db:
            before = db.execute('SELECT * FROM notification').fetchall()
        await self.state.set_enabled(self.path, 11, False)
        await self.state.set_enabled(self.path, 11, True, self.now)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT * FROM notification').fetchall(), before)
            self.assertEqual(db.execute('SELECT value FROM runtime_setting WHERE key=?',
                                       ('notification_delay_seconds:11',)).fetchone(), ('180',))
        self.assertNotIn('22', self.state.states)

    async def test_pausing_clears_only_that_servers_pending_and_ready(self):
        self.tracker.pending_batches('example', 'client', [self.before], [11, 22], self.now)
        self.tracker.ready_tweets[('client','example','11')] = [self.before]
        await self.tracker.set_server_enabled(11, False)
        self.assertNotIn(('client','example','11'), self.tracker.pending_tweets)
        self.assertNotIn(('client','example','11'), self.tracker.ready_tweets)
        self.assertEqual(len(self.tracker.pending_tweets[('client','example','22')]), 1)
        self.tracker.pending_batches('example', 'client', [self.after], [11,22], self.now)
        self.assertNotIn(('client','example','11'), self.tracker.pending_tweets)

    async def test_resume_ignores_cached_posts_from_disabled_period(self):
        await self.state.set_enabled(self.path, 11, False)
        self.tracker.pending_batches('example', 'client', [self.before], [11,22], self.now)
        await self.state.set_enabled(self.path, 11, True, self.now)
        self.tracker.pending_batches('example', 'client', [self.before,self.after], [11,22], self.now)
        self.assertEqual(self.tracker.pending_tweets[('client','example','11')].tweets(), [self.after])
        self.assertEqual(self.tracker.pending_tweets[('client','example','22')].tweets(), [self.before,self.after])

    async def test_disable_finishes_inflight_tweet_then_confirms_and_blocks_new_sends(self):
        async with self.state.delivery(11, self.before) as allowed:
            self.assertTrue(allowed)
            disable = asyncio.create_task(self.tracker.set_server_enabled(11, False))
            # Wait for the persisted pause/drain point, not a wall-clock sleep.
            for _ in range(1000):
                if not self.state.enabled(11):
                    break
                await asyncio.sleep(0)
            self.assertFalse(self.state.enabled(11))
            self.assertFalse(disable.done())
            async with self.state.delivery(11, self.after) as new_allowed:
                self.assertFalse(new_allowed)
            async with self.state.delivery(22, self.before) as other_allowed:
                self.assertTrue(other_allowed)
        self.assertTrue(await asyncio.wait_for(disable, 2))

    async def test_failed_send_releases_inflight_count(self):
        with self.assertRaises(RuntimeError):
            async with self.state.delivery(11, self.before):
                raise RuntimeError('test failure')
        await asyncio.wait_for(self.state.drain(11), 1)
        self.assertEqual(self.state.active['11'], 0)

    async def test_failed_persistence_rolls_back_pause(self):
        with patch('src.notification.server_delivery.aiosqlite.connect', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                await self.state.set_enabled(self.path, 11, False)
        self.assertTrue(self.state.enabled(11))


class ServerDeliveryCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {'TWITTER_TOKEN':'Account1:test-token'}):
            from src.i18n import init_i18n
            init_i18n('en')
            self.Notification = importlib.import_module('cogs.notification').Notification
        self.cog = self.Notification.__new__(self.Notification)
        self.cog.account_tracker = NS(set_server_enabled=AsyncMock(return_value=True))
        self.interaction = NS(guild_id=11, response=NS(defer=AsyncMock(),send_message=AsyncMock()),
                              followup=NS(send=AsyncMock()))

    async def test_disable_and_enable_pass_invoking_server_only(self):
        await self.Notification.disable_server.callback(self.cog, self.interaction)
        self.cog.account_tracker.set_server_enabled.assert_awaited_with(11, False)
        self.assertIn('preserved', self.interaction.followup.send.call_args.args[0])
        await self.Notification.enable_server.callback(self.cog, self.interaction)
        self.cog.account_tracker.set_server_enabled.assert_awaited_with(11, True)
        self.assertIn('saved accounts and settings', self.interaction.followup.send.call_args.args[0])

    async def test_dm_does_not_change_any_server(self):
        self.interaction.guild_id = None
        await self.cog._set_server_enabled(self.interaction, False)
        self.cog.account_tracker.set_server_enabled.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()

    async def test_commands_require_manage_server_and_are_guild_only(self):
        for command in (self.Notification.disable_server, self.Notification.enable_server):
            self.assertTrue(command.guild_only)
            self.assertTrue(command.default_permissions.manage_guild)
            self.interaction.permissions = discord.Permissions.none()
            with self.assertRaises(discord.app_commands.MissingPermissions):
                await command.checks[0](self.interaction)

    async def test_redundant_command_has_clear_response(self):
        self.cog.account_tracker.set_server_enabled.return_value = False
        await self.cog._set_server_enabled(self.interaction, False)
        self.assertIn('already disabled', self.interaction.followup.send.call_args.args[0])
