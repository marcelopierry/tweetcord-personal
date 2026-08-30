import os
import sys
import tempfile
import unittest

import aiosqlite

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.delivery_history import DeliveryHistory


class TestDeliveryHistory(unittest.IsolatedAsyncioTestCase):
    async def test_claim_record_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, 'history.db')
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    'CREATE TABLE delivered_tweet ('
                    'channel_id TEXT, tweet_id TEXT, delivered_at TEXT NOT NULL, '
                    'PRIMARY KEY(channel_id, tweet_id))'
                )
                await db.commit()

            history = DeliveryHistory(db_path)
            self.assertTrue(await history.claim('channel-a', 'tweet-1'))
            self.assertFalse(await history.claim('channel-a', 'tweet-1'))
            self.assertTrue(await history.claim('channel-b', 'tweet-1'))

            await history.record('channel-a', 'tweet-2', 'tweet-3')
            self.assertFalse(await history.claim('channel-a', 'tweet-2'))

            await history.release('channel-a', 'tweet-1')
            self.assertTrue(await history.claim('channel-a', 'tweet-1'))


if __name__ == '__main__':
    unittest.main()
