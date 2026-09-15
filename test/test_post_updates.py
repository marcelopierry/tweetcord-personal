import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from core.classes import ParsedTweet
from src.notification.account_tracker import AccountTracker
from src.notification.delay_queue import DelayedTweetBuffer
from src.notification.delivery import build_tweet_embed, build_quote_original_embed, post_timestamp


class TimestampTests(unittest.TestCase):
    def test_retweet_uses_refreshed_original_time_without_nested_source(self):
        parsed = ParsedTweet({'tweet': {'id': '2099233287784821194', 'created_timestamp': 1789331192,
            'raw_text': {'text': 'Original'}, 'author': {'name':'Original','screen_name':'original'}}})
        tweet = NS(is_retweet=True, retweeted_tweet=None, created_on=datetime(2026,9,15,tzinfo=timezone.utc))
        embed = build_tweet_embed('retweeter', tweet, parsed, 'Original')
        self.assertEqual(embed.timestamp, datetime.fromtimestamp(1789331192,timezone.utc))
        self.assertIsNone(embed.footer.text)

    def test_quote_cards_have_distinct_own_timestamps(self):
        parsed = ParsedTweet({'tweet': {'id':'2099233287784821194', 'created_timestamp':1789331192,
            'raw_text': {'text':'Comment'}, 'author': {'name':'Q','screen_name':'q'},
            'quote': {'created_timestamp':1789330000, 'url':'https://x.com/o/status/2099233287784821193',
                      'raw_text':{'text':'Original'}, 'author':{'name':'O','screen_name':'o'}}}})
        tweet=NS(is_retweet=False,is_quoted=True,quoted_tweet=None,author=NS(name='Q',username='q'))
        first=build_tweet_embed('q',tweet,parsed,'Comment')
        second=build_quote_original_embed(tweet,parsed)
        self.assertNotEqual(first.timestamp,second.timestamp)
        self.assertEqual(second.timestamp,datetime.fromtimestamp(1789330000,timezone.utc))
        self.assertIsNone(second.footer.text)

    def test_snowflake_fallback_and_invalid_time(self):
        expected=datetime(2026,9,13,20,26,32,tzinfo=timezone.utc)
        actual=post_timestamp('bad-date',url='https://x.com/a/status/2099233287784821194')
        self.assertLess(abs((actual-expected).total_seconds()),1)


class OriginalPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.tracker=AccountTracker.__new__(AccountTracker)
        self.tracker.db_path=self.temp.name+'/test.db'
        self.tracker.pending_tweets={}
        self.tracker.ready_tweets={}
        with sqlite3.connect(self.tracker.db_path) as db:
            db.executescript('CREATE TABLE user(id TEXT,enabled INTEGER); CREATE TABLE notification(user_id TEXT,channel_id TEXT,enabled INTEGER,enable_type TEXT,enable_media_type TEXT); INSERT INTO user VALUES("a",1); INSERT INTO notification VALUES("a","1",1,"11","11");')
        self.original=NS(id='100',is_retweet=False,is_quoted=False,author=NS(id='a'))
        self.retweet=NS(id='200',is_retweet=True,is_quoted=False,retweeted_tweet=self.original)
        self.parsed=ParsedTweet({'tweet':{'id':'100','media':{},'raw_text':{'text':'Original'}}})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_not_yet_due_original_wins(self):
        buffer=DelayedTweetBuffer(180); buffer.add(self.original)
        self.tracker.pending_tweets[('client','a')]=buffer
        self.assertTrue(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,1))

    async def test_inflight_original_wins_after_pop(self):
        self.tracker.ready_tweets[('client','a')]=[self.original]
        self.assertTrue(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,1))

    async def test_different_channel_does_not_suppress(self):
        self.tracker.ready_tweets[('client','a')]=[self.original]
        self.assertFalse(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,2))

    async def test_removed_original_or_retweet_alone_does_not_suppress(self):
        self.assertFalse(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,1))
        self.tracker.ready_tweets[('client','a')]=[self.original]
        with sqlite3.connect(self.tracker.db_path) as db:
            db.execute('UPDATE notification SET enabled=0')
        self.assertFalse(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,1))

    async def test_media_filter_does_not_block_eligible_retweet(self):
        self.tracker.ready_tweets[('client','a')]=[self.original]
        with sqlite3.connect(self.tracker.db_path) as db:
            db.execute('UPDATE notification SET enable_media_type="01"')
        self.assertFalse(await self.tracker.queued_original_for_channel(self.retweet,self.parsed,1))
