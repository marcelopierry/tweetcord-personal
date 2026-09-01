import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

import aiosqlite

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.delivery_history import DeliveryHistory
from src.notification.delivery import get_delivery_references


class TestDeliveryHistory(unittest.IsolatedAsyncioTestCase):
    async def _create_history(self, directory: str) -> DeliveryHistory:
        db_path = os.path.join(directory, 'history.db')
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                'CREATE TABLE delivered_tweet ('
                'channel_id TEXT, tweet_id TEXT, delivered_at TEXT NOT NULL, '
                'PRIMARY KEY(channel_id, tweet_id))'
            )
            await db.commit()
        return DeliveryHistory(db_path)

    async def test_claim_record_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            history = await self._create_history(directory)
            self.assertTrue(await history.claim('channel-a', 'tweet-1'))
            self.assertFalse(await history.claim('channel-a', 'tweet-1'))
            self.assertTrue(await history.claim('channel-b', 'tweet-1'))

            await history.record('channel-a', 'tweet-2', 'tweet-3')
            self.assertFalse(await history.claim('channel-a', 'tweet-2'))

            await history.release('channel-a', 'tweet-1')
            self.assertTrue(await history.claim('channel-a', 'tweet-1'))

    async def test_quote_context_does_not_suppress_a_later_retweet(self):
        with tempfile.TemporaryDirectory() as directory:
            history = await self._create_history(directory)
            original = SimpleNamespace(id='100', url='https://x.com/original/status/100')
            quote = SimpleNamespace(
                id='200', url='https://x.com/quoter/status/200',
                is_retweet=False, is_quoted=True, quoted_tweet=original,
            )
            retweet = SimpleNamespace(
                id='300', url='https://x.com/retweeter/status/300',
                is_retweet=True, is_quoted=False, retweeted_tweet=original,
            )
            quote_refs = get_delivery_references(quote, None)
            retweet_refs = get_delivery_references(retweet, None)

            self.assertTrue(await history.claim('channel-a', quote_refs.claim_id))
            await history.record('channel-a', *quote_refs.record_ids)
            self.assertTrue(await history.claim('channel-a', retweet_refs.claim_id))

    async def test_direct_original_suppresses_a_later_retweet(self):
        with tempfile.TemporaryDirectory() as directory:
            history = await self._create_history(directory)
            original = SimpleNamespace(
                id='100', url='https://x.com/original/status/100',
                is_retweet=False, is_quoted=False, quoted_tweet=None,
            )
            retweet = SimpleNamespace(
                id='300', url='https://x.com/retweeter/status/300',
                is_retweet=True, is_quoted=False, retweeted_tweet=original,
            )
            original_refs = get_delivery_references(original, None)
            retweet_refs = get_delivery_references(retweet, None)

            self.assertTrue(await history.claim('channel-a', original_refs.claim_id))
            await history.record('channel-a', *original_refs.record_ids)
            self.assertFalse(await history.claim('channel-a', retweet_refs.claim_id))


if __name__ == '__main__':
    unittest.main()
