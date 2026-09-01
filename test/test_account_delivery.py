import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.account_tracker import AccountTracker
from src.notification.delivery import MediaDelivery


class TestAccountDeliveryPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_quote_delivery_orders_both_media_groups_and_only_previews_video_links(self):
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.session = SimpleNamespace()
        tracker.delivery = SimpleNamespace(send=AsyncMock())
        tracker.delivery_history = SimpleNamespace(
            claim=AsyncMock(return_value=True),
            record=AsyncMock(),
            release=AsyncMock(),
        )

        original_author = SimpleNamespace(
            name='Original Author',
            username='original',
            profile_image_url_https='https://pbs.twimg.com/original.jpg',
        )
        original = SimpleNamespace(
            id='100',
            url='https://x.com/original/status/100',
            text='Original text',
            author=original_author,
            created_on=None,
        )
        tweet = SimpleNamespace(
            id='200',
            url='https://x.com/quoter/status/200',
            text='Commentary',
            author=SimpleNamespace(name='Quoter', username='quoter'),
            created_on=None,
            is_retweet=False,
            is_quoted=True,
            quoted_tweet=original,
        )
        parsed = SimpleNamespace(
            get_text=lambda simplified_content: (
                'Commentary [article](https://example.com/story) https://youtu.be/video123',
                False,
            ),
            sender_name='Quoter',
            sender_username='quoter',
            sender_avatar_url='https://pbs.twimg.com/quoter.jpg',
            author_name='Quoter',
            author_username='quoter',
            author_avatar_url='https://pbs.twimg.com/quoter.jpg',
            media=SimpleNamespace(items=[{'type': 'photo'}]),
            quote_media=SimpleNamespace(items=[{'type': 'video'}]),
            quote=SimpleNamespace(
                text='Original text',
                trans_text=None,
                name='Original Author',
                screen_name='original',
                url=original.url,
                avatar_url='https://pbs.twimg.com/original.jpg',
            ),
        )
        channel = SimpleNamespace(
            id=321,
            mention='<#321>',
            guild=SimpleNamespace(filesize_limit=25_000_000, get_role=lambda role_id: None),
        )
        data = {'role_id': None, 'customized_msg': None}
        media_deliveries = [
            MediaDelivery(files=[], fallback_urls=['https://media.example/quote.jpg']),
            MediaDelivery(files=[], fallback_urls=['https://media.example/original.mp4']),
        ]

        with patch(
            'src.notification.account_tracker.prepare_media_delivery',
            new=AsyncMock(side_effect=media_deliveries),
        ):
            await tracker._deliver_tweet_to_channel(
                'quoter', tweet, data, channel, parsed, None, None,
            )

        contents = [call.kwargs['content'] for call in tracker.delivery.send.await_args_list]
        self.assertEqual(contents, [
            '<https://x.com/quoter/status/200>\n🔁 <https://x.com/original/status/100>',
            '▷\nhttps://media.example/quote.jpg',
            '▷\nhttps://media.example/original.mp4',
            '↧\nhttps://youtu.be/video123',
        ])
        self.assertFalse(any('https://example.com/story' == content for content in contents[1:]))
        tracker.delivery_history.record.assert_awaited_once_with(321, 'post:200')


if __name__ == '__main__':
    unittest.main()
