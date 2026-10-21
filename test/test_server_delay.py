import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from src.notification.account_tracker import AccountTracker
from src.notification.server_delay import ServerDelays
from src.notification.server_delivery import ServerDelivery
from src.notification.delivery import ChannelDeliverySequencer
from src.notification.utils import TweetUnavailable


class ServerDelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {'TWITTER_TOKEN': 'Account1:test-token'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from src.i18n import init_i18n
        init_i18n('en')
        self.Notification = importlib.import_module('cogs.notification').Notification
        self.path = self.temp.name + '/test.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE runtime_setting(key TEXT PRIMARY KEY,value TEXT);'
                             'CREATE TABLE channel(id TEXT,server_id TEXT);'
                             'INSERT INTO channel VALUES("101","11");'
                             'INSERT INTO runtime_setting VALUES("notification_delay_seconds","60");')
        self.tracker = AccountTracker.__new__(AccountTracker)
        self.tracker.db_path = self.path
        self.tracker.pending_tweets = {}
        self.tracker.ready_tweets = {}
        self.tracker.server_delays = ServerDelays(180)
        self.tracker.server_delivery = ServerDelivery()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.tweet = NS(id='100', created_on=self.now, url='https://x.com/example/status/100',
                        author=NS(username='example'), is_retweet=False)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_migrates_current_global_for_existing_servers_only_once(self):
        delays = self.tracker.server_delays
        await delays.load(self.path, [22])
        self.assertEqual((delays.get(11), delays.get(22)), (60, 60))
        await delays.set(self.path, 11, 240)
        restarted = ServerDelays(180)
        await restarted.load(self.path, [11, 22, 33])
        self.assertEqual([restarted.get(i) for i in (11, 22, 33)], [240, 60, 180])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT value FROM runtime_setting WHERE key=?',
                                       ('notification_delay_seconds',)).fetchone()[0], '60')

    async def test_existing_scoped_setting_survives_migration(self):
        await self.tracker.server_delays.set(self.path, 11, 420)
        await self.tracker.server_delays.load(self.path, [22])
        self.assertEqual(self.tracker.server_delays.get(11), 420)
        self.assertEqual(self.tracker.server_delays.get(22), 60)

    async def test_invalid_legacy_setting_uses_default(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE runtime_setting SET value="invalid"')
        await self.tracker.server_delays.load(self.path)
        self.assertEqual(self.tracker.server_delays.get(11), 180)

    async def test_independent_release_with_no_new_candidates(self):
        self.tracker.server_delays.values = {'11': 0, '22': 300}
        first = self.tracker.pending_batches('example', 'client', [self.tweet], [11, 22], self.now)
        self.assertEqual([row[0] for row in first], ['11'])
        # The shared account watermark can now advance. Server 22 must retain its copy.
        second = self.tracker.pending_batches('example', 'client', None, [11, 22],
                                              self.now + timedelta(seconds=300))
        self.assertEqual([row[0] for row in second], ['22'])
        self.assertEqual(second[0][2], [self.tweet])

    async def test_retroactive_changes_only_reschedule_target_server(self):
        self.tracker.pending_batches('example', 'client', [self.tweet], [11, 22], self.now)
        self.assertEqual(await self.tracker.set_notification_delay(11, 60), 1)
        batches = self.tracker.pending_batches('example', 'client', [], [11, 22],
                                               self.now + timedelta(seconds=60))
        self.assertEqual([row[0] for row in batches], ['11'])
        self.assertEqual(len(self.tracker.pending_tweets[('client', 'example', '22')]), 1)
        self.assertEqual(await self.tracker.set_notification_delay(22, 600), 1)
        self.assertEqual(self.tracker.pending_batches('example', 'client', [], [11, 22],
                                                     self.now + timedelta(seconds=180)), [])
        batches = self.tracker.pending_batches('example', 'client', [], [11, 22],
                                               self.now + timedelta(seconds=600))
        self.assertEqual([row[0] for row in batches], ['22'])

    async def test_removing_destination_discards_only_its_queue(self):
        self.tracker.pending_batches('example', 'client', [self.tweet], [11, 22], self.now)
        self.tracker.pending_batches('example', 'client', [], [22], self.now)
        self.assertNotIn(('client', 'example', '11'), self.tracker.pending_tweets)
        self.assertIn(('client', 'example', '22'), self.tracker.pending_tweets)

    async def test_real_delivery_loop_routes_and_validates_at_each_servers_deadline(self):
        tracker = self.tracker
        tracker.server_delays.values = {'11': 0, '22': 300}
        tracker.latest_tweet_timestamps = {('example', 'client'): str(self.now - timedelta(days=1))}
        tracker.tweets = {'client': [self.tweet]}
        tracker.db_write_queue = asyncio.Queue()
        tracker.session = None
        tracker.delivery_sequencer = ChannelDeliverySequencer()
        tracker.bot = NS(get_channel=lambda channel_id: NS(id=channel_id))
        tracker.notification_settings = AsyncMock(return_value={
            str(server): [dict(channel_id=str(server*10), enable_type='11',
                               enable_media_type='11', server_translate=None)] for server in (11, 22)})
        tracker._refresh_ready_tweet = AsyncMock(return_value=('available', NS(media=NS(type=None))))
        tracker.queued_original_for_channel = AsyncMock(return_value=False)
        tracker._deliver_tweet_to_channel = AsyncMock()

        async def cycle():
            with patch('src.notification.account_tracker.asyncio.sleep',
                       AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                 patch('src.notification.account_tracker.is_match_type', return_value=True), \
                 patch('src.notification.account_tracker.is_match_media_type', return_value=True):
                with self.assertRaises(asyncio.CancelledError):
                    await tracker.notification('example', 'client')

        await cycle()
        self.assertEqual(tracker._deliver_tweet_to_channel.call_args.args[3].id, 110)
        self.assertEqual(tracker._refresh_ready_tweet.call_args.args[-1], '11')
        self.assertEqual(tracker.db_write_queue.qsize(), 1)
        # No candidates after the shared watermark advances; the other queue still releases.
        await tracker.set_notification_delay(22, 0)
        await cycle()
        self.assertEqual(tracker._deliver_tweet_to_channel.call_count, 2)
        self.assertEqual(tracker._deliver_tweet_to_channel.call_args.args[3].id, 220)
        self.assertEqual(tracker._refresh_ready_tweet.call_args.args[-1], '22')

    async def test_original_in_another_server_does_not_defer_retweet(self):
        original = NS(id='100', is_retweet=False, is_quoted=False, author=NS(id='a'))
        retweet = NS(id='200', is_retweet=True, is_quoted=False, retweeted_tweet=original)
        self.tracker.ready_tweets[('client', 'original', '22')] = [original]
        self.assertFalse(await self.tracker.queued_original_for_channel(retweet, None, 110, '11'))

    async def test_deletion_confirmations_are_separate_for_each_server(self):
        tracker = self.tracker
        tracker.x_validation_lock = asyncio.Lock()
        tracker.x_clients = {'client': NS()}
        tracker.unavailable_checks = {}
        pending = NS(defer=Mock())
        with patch('src.notification.account_tracker.validate_on_x',
                   AsyncMock(side_effect=TweetUnavailable('unavailable'))):
            first, _ = await tracker._refresh_ready_tweet(pending, self.tweet, 'example', 'client', '11')
            other, _ = await tracker._refresh_ready_tweet(pending, self.tweet, 'example', 'client', '22')
            confirmed, _ = await tracker._refresh_ready_tweet(pending, self.tweet, 'example', 'client', '11')
        self.assertEqual((first, other, confirmed), ('deferred', 'deferred', 'deleted'))

    async def test_command_passes_invoking_server(self):
        cog = self.Notification.__new__(self.Notification)
        cog.account_tracker = NS(set_notification_delay=AsyncMock(return_value=2))
        interaction = NS(guild_id=11, response=NS(defer=AsyncMock()),
                         followup=NS(send=AsyncMock()))
        await cog._set_send_delay(interaction, 4)
        cog.account_tracker.set_notification_delay.assert_awaited_once_with(11, 240)
        self.assertNotIn('Other servers', interaction.followup.send.call_args.args[0])
        self.assertIn('Updated **2** currently queued tweet(s)', interaction.followup.send.call_args.args[0])

    async def test_command_in_dm_does_not_change_any_delay(self):
        cog = self.Notification.__new__(self.Notification)
        cog.account_tracker = NS(set_notification_delay=AsyncMock())
        interaction = NS(guild_id=None, response=NS(send_message=AsyncMock()))
        await cog._set_send_delay(interaction, 4)
        cog.account_tracker.set_notification_delay.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
