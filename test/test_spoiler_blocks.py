import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from src.db_function.init_db import init_db
from src.notification.account_tracker import AccountTracker
from src.notification.channel_filter import GIANTS_CHANNEL_FILTER
from src.notification.spoiler_blocks import SpoilerBlocks


class SpoilerBlockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        with patch.dict(os.environ, {'DATA_PATH': directory}):
            await init_db()
        self.path = os.path.join(directory, 'tracked_accounts.db')
        self.blocks = SpoilerBlocks(self.path)
        await self.blocks.load()

    async def test_defaults_match_substrings_case_insensitively_across_this_server(self):
        rule = await self.blocks.add(1, '  Doomsday  ')
        self.assertIsNone(rule.expires_at)
        for channel, account, body in (
            (21, 'Marvel', '#AvengersDoomsday'),
            (22, 'DiscussingFilm', 'dOOmSday'),
        ):
            self.assertEqual(self.blocks.blocked_rule(1, channel, account, NS(text=body)), rule)
        self.assertIsNone(self.blocks.blocked_rule(2, 31, 'Marvel', NS(text='doomsday')))
        self.assertIsNone(self.blocks.blocked_rule(1, 21, 'Marvel', NS(text='Avengers')))

    async def test_optional_account_and_channel_scopes(self):
        tweet = NS(text='Doomsday')
        scoped = await self.blocks.add(1, 'Doomsday', account='@Marvel', channel_id=21)
        self.assertEqual(self.blocks.blocked_rule(1, 21, 'MARVEL', tweet), scoped)
        self.assertIsNone(self.blocks.blocked_rule(1, 22, 'Marvel', tweet))
        self.assertIsNone(self.blocks.blocked_rule(1, 21, 'Other', tweet))
        everywhere_for_account = await self.blocks.add(1, 'Doomsday', account='Marvel')
        self.assertEqual(self.blocks.blocked_rule(1, 22, 'Marvel', tweet), everywhere_for_account)
        all_accounts_in_channel = await self.blocks.add(1, 'Doomsday', channel_id=21)
        self.assertEqual(self.blocks.blocked_rule(1, 21, 'Other', tweet), all_accounts_in_channel)

    async def test_fresh_quote_repost_and_formatted_phrase_text(self):
        rule = await self.blocks.add(1, 'Avengers Doomsday')
        repost = NS(text='RT @Someone: unrelated', retweeted_tweet=NS(text='AVENGERS DOOMSDAY'))
        self.assertEqual(self.blocks.blocked_rule(1, 21, 'Marvel', repost), rule)
        fresh = NS(text='new caption', quote=NS(text='**Avengers**\n[Doomsday](https://x.com/hashtag/Doomsday)'))
        self.assertEqual(self.blocks.blocked_rule(1, 21, 'Marvel', NS(text='old caption'), fresh), rule)
        edited = NS(text='new caption', quote=NS(text=''))
        stale = NS(text='Avengers Doomsday', quoted_tweet=NS(text='Avengers Doomsday'))
        self.assertIsNone(self.blocks.blocked_rule(1, 21, 'Marvel', stale, edited))

    async def test_formatted_hashtag_preserves_literal_punctuation(self):
        parsed = NS(text='[#Avengers_Doomsday](https://x.com/hashtag/Avengers_Doomsday)')
        await self.blocks.add(1, 'AvengersDoomsday')
        self.assertIsNone(self.blocks.blocked_rule(1, 21, 'Marvel', NS(), parsed))
        exact = await self.blocks.add(1, 'Avengers_Doomsday')
        self.assertEqual(self.blocks.blocked_rule(1, 21, 'Marvel', NS(), parsed), exact)

    async def test_expiry_update_persistence_and_guild_scoped_removal(self):
        clock = NS(time=lambda: 1000)
        with patch('src.notification.spoiler_blocks.time', clock):
            rule = await self.blocks.add(1, 'Doomsday', days=2)
            self.assertEqual(rule.expires_at, 173800)
            other = await self.blocks.add(2, 'Doomsday')
            reopened = SpoilerBlocks(self.path)
            await reopened.load()
            self.assertEqual(reopened.active_rules(1), [rule])
            self.assertFalse(await reopened.remove(2, rule.id))
            clock.time = lambda: 173799
            self.assertEqual(reopened.active_rules(1), [rule])
            clock.time = lambda: 173800
            self.assertEqual(reopened.active_rules(1), [])
            self.assertIsNone(reopened.blocked_rule(1, 21, 'Marvel', NS(text='Doomsday')))
            updated = await reopened.add(1, 'dOOmSday')
            self.assertEqual(updated.id, rule.id)
            self.assertIsNone(updated.expires_at)
            self.assertTrue(await reopened.remove(1, rule.id))
            await self.blocks.load()
            self.assertEqual(self.blocks.active_rules(1), [])
            self.assertEqual(self.blocks.active_rules(2), [other])

    async def test_blank_rule_or_invalid_handle_does_not_create_server_wide_filter(self):
        for term, account in (('   ', None), ('Doomsday', '@'), ('Doomsday', 'example.com')):
            with self.assertRaises(ValueError):
                await self.blocks.add(1, term, account=account)
        self.assertEqual(self.blocks.active_rules(1), [])

    async def test_delivery_blocks_before_claim_even_with_giants_exemption(self):
        await self.blocks.add(1, 'Doomsday')
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.spoiler_blocks = self.blocks
        tracker.delivery_history = NS(claim=AsyncMock(return_value=False))
        tracker.delivery = NS(send=AsyncMock())
        channel = NS(id=int(GIANTS_CHANNEL_FILTER.channel_id), guild=NS(id=1))
        tweet = NS(id='100', text='old version', is_retweet=False, is_quoted=True)
        fresh = NS(text='#AvengersDoomsday')
        await tracker._deliver_tweet_to_channel('Giants', tweet, {}, channel, None, None, None,
                                                fresh_parsed_tweet=fresh)
        tracker.delivery_history.claim.assert_not_awaited()
        tracker.delivery.send.assert_not_awaited()

        other_channel = NS(id=22, guild=NS(id=2))
        await tracker._deliver_tweet_to_channel('Giants', tweet, {}, other_channel, None, None, None,
                                                fresh_parsed_tweet=fresh)
        tracker.delivery_history.claim.assert_awaited_once()
