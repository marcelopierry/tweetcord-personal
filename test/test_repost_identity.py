import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
import discord

from src.notification.delivery import TweetDelivery, get_delivery_references
from src.notification.delivery_history import DeliveryHistory


class RepostHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_and_repost_share_identity_but_not_quoted_context(self):
        with tempfile.TemporaryDirectory() as directory:
            history = DeliveryHistory(os.path.join(directory, 'history.db'))
            async with aiosqlite.connect(history.db_path) as db:
                await db.execute('CREATE TABLE delivered_tweet(channel_id TEXT, tweet_id TEXT, delivered_at TEXT, PRIMARY KEY(channel_id,tweet_id))')
                await db.commit()
            original = NS(id='100', url='https://x.com/a/status/100')
            quote = NS(id='200', is_retweet=False, is_quoted=True, quoted_tweet=original)
            repost = NS(id='300', is_retweet=True, is_quoted=True, retweeted_tweet=quote)
            # Existing history from the previous version must also suppress it.
            await history.record('a', 'post:200')
            self.assertFalse(await history.claim('a', get_delivery_references(repost, None).claim_id))
            self.assertTrue(await history.claim('b', get_delivery_references(repost, None).claim_id))
            # A repost delivered first suppresses later direct delivery of that quote.
            self.assertFalse(await history.claim('b', get_delivery_references(quote, None).claim_id))
            # Showing the context has not marked the context post as delivered.
            self.assertTrue(await history.claim('a', 'original:100'))
            # New quote commentary still sends even after its context was sent.
            quote.id = '400'
            self.assertTrue(await history.claim('a', get_delivery_references(quote, None).claim_id))
            # Aliased claims serialize atomically, too.
            result = await asyncio.gather(history.claim('a', 'post:500'), history.claim('a', 'original:500'))
            self.assertEqual(sum(result), 1)


class IdentityRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_preserves_identity_and_rewinds_files(self):
        error = discord.HTTPException(NS(status=429, reason='Too Many Requests', headers={'Retry-After':'8'}), {'code':40062, 'message':'rate limited'})
        webhook = NS(send=AsyncMock(side_effect=[error, error, None]))
        channel = NS(id=123, send=AsyncMock())
        delivery = TweetDelivery(NS())
        delivery._get_webhook = AsyncMock(return_value=webhook)
        file = Mock()
        with patch('src.notification.delivery.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await delivery.send(channel, content='tweet', username='Mike | Personal TweetCord', avatar_url='https://example.com/avatar.png', files=[file])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [8, 10])
        self.assertEqual(file.reset.call_count, 2)
        self.assertEqual(webhook.send.await_count, 3)
        channel.send.assert_not_awaited()
        for call in webhook.send.await_args_list:
            self.assertEqual(call.kwargs['username'], 'Mike | Personal TweetCord')
            self.assertEqual(call.kwargs['avatar_url'], 'https://example.com/avatar.png')
            self.assertTrue(call.kwargs['wait'])

    async def test_rate_limit_wait_can_be_cancelled_without_bot_fallback(self):
        error = discord.HTTPException(NS(status=429, reason='Too Many Requests', headers={}), 'rate limited')
        channel = NS(id=123, send=AsyncMock())
        delivery = TweetDelivery(NS())
        delivery._get_webhook = AsyncMock(return_value=NS(send=AsyncMock(side_effect=error)))
        with patch('src.notification.delivery.asyncio.sleep', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await delivery.send(channel, content='tweet', username='Mike', avatar_url=None)
        channel.send.assert_not_awaited()
