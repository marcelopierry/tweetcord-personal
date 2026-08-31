import os
import sys
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.classes import ParsedTweet
from src.notification.delivery import TweetDelivery, _ordered_candidates, build_delivery_links, build_delivery_text, build_quote_original_embed, build_tweet_embed, build_webhook_identity, extract_youtube_urls, get_delivery_references, media_candidates


class TestTweetDeliveryHelpers(unittest.TestCase):
    def test_quote_delivers_quote_then_original_link(self):
        parsed = SimpleNamespace(quote=SimpleNamespace(url='https://x.com/original/status/1'))
        links = build_delivery_links('https://x.com/tracked/status/2', parsed, is_quote=True)
        self.assertEqual(links, '<https://x.com/tracked/status/2>\n🔁 <https://x.com/original/status/1>')

    def test_retweet_links_to_original_with_retweet_emoji(self):
        links = build_delivery_links(
            'https://x.com/tracked/status/2',
            None,
            is_quote=False,
            is_retweet=True,
            original_url='https://x.com/original/status/1',
        )
        self.assertEqual(links, '🔄 <https://x.com/original/status/1>')

    def test_delivery_text_uses_parsed_tweet_body(self):
        parsed = SimpleNamespace(get_text=lambda simplified_content: ('The tweet body', False))
        tweet = SimpleNamespace(text='fallback')
        self.assertEqual(build_delivery_text(tweet, parsed), 'The tweet body')

    def test_delivery_text_caps_long_fallback(self):
        tweet = SimpleNamespace(text='x' * 1600)
        self.assertLessEqual(len(build_delivery_text(tweet, None)), 1200)

    def test_youtube_links_are_extracted_once_for_separate_previews(self):
        text = (
            'Watch https://youtu.be/abc123. Then '
            'https://www.youtube.com/watch?v=xyz789&feature=share and '
            'again https://youtu.be/abc123'
        )
        self.assertEqual(extract_youtube_urls(text), [
            'https://youtu.be/abc123',
            'https://www.youtube.com/watch?v=xyz789&feature=share',
        ])

    def test_youtube_shorts_and_mobile_links_are_supported(self):
        text = 'https://youtube.com/shorts/abc123 https://m.youtube.com/watch?v=xyz789'
        self.assertEqual(extract_youtube_urls(text), [
            'https://youtube.com/shorts/abc123',
            'https://m.youtube.com/watch?v=xyz789',
        ])

    def test_retweet_claims_original_id_for_deduplication(self):
        original = SimpleNamespace(id='100', url='https://x.com/original/status/100')
        tweet = SimpleNamespace(id='200', url='https://x.com/tracked/status/200', is_retweet=True, is_quoted=False, retweeted_tweet=original)
        references = get_delivery_references(tweet, None)
        self.assertEqual(references.claim_id, '100')
        self.assertEqual(references.tweet_id, '200')

    def test_quote_claims_its_own_id_even_when_original_was_seen(self):
        original = SimpleNamespace(id='100', url='https://x.com/original/status/100')
        tweet = SimpleNamespace(id='200', url='https://x.com/tracked/status/200', is_retweet=False, is_quoted=True, quoted_tweet=original)
        references = get_delivery_references(tweet, None)
        self.assertEqual(references.claim_id, '200')
        self.assertEqual(references.original_id, '100')

    def test_quote_original_is_rendered_as_a_second_card(self):
        source = SimpleNamespace(
            text='Fallback original text',
            url='https://x.com/original/status/100',
            created_on=None,
            author=SimpleNamespace(
                name='Original Author',
                username='original',
                profile_image_url_https='https://pbs.twimg.com/profile_images/original_normal.jpg',
            ),
        )
        tweet = SimpleNamespace(is_quoted=True, quoted_tweet=source)
        parsed = SimpleNamespace(quote=SimpleNamespace(
            text='Original tweet text',
            trans_text=None,
            name='Original Author',
            screen_name='original',
            url=source.url,
            avatar_url='https://pbs.twimg.com/profile_images/original_normal.jpg',
        ))
        embed = build_quote_original_embed(tweet, parsed)
        self.assertEqual(embed.description, 'Original tweet text')
        self.assertEqual(embed.author.name, 'Original Author (@original)')
        self.assertEqual(embed.author.url, source.url)
        self.assertEqual(embed.author.icon_url, 'https://pbs.twimg.com/profile_images/original_400x400.jpg')
        self.assertEqual(embed.color.value, 0xAAB8C2)

    def test_non_quote_has_no_original_card(self):
        self.assertIsNone(build_quote_original_embed(SimpleNamespace(is_quoted=False), None))

    def test_webhook_identity_uses_retweeter_identity(self):
        parsed = SimpleNamespace(
            sender_name='Discussing Film',
            sender_username='DiscussingFilm',
            sender_avatar_url='https://pbs.twimg.com/profile_images/example_normal.jpg',
        )
        tweet = SimpleNamespace(author=SimpleNamespace(name='Original Author', username='Original', profile_image_url_https=None))
        name, avatar = build_webhook_identity('DiscussingFilm', tweet, parsed)
        self.assertEqual(name, 'Discussing Film | Personal TweetCord')
        self.assertEqual(avatar, 'https://pbs.twimg.com/profile_images/example_400x400.jpg')

    def test_tweet_text_is_rendered_in_a_compact_embed_card(self):
        parsed = SimpleNamespace(
            sender_name='Bobby Skinner',
            sender_username='BobbySkinner_',
            sender_avatar_url='https://pbs.twimg.com/profile_images/example_normal.jpg',
        )
        tweet = SimpleNamespace(
            author=SimpleNamespace(name='Bobby Skinner', username='BobbySkinner_', profile_image_url_https=None),
            created_on=None,
        )
        embed = build_tweet_embed('BobbySkinner_', tweet, parsed, 'Tweet body in a box')
        self.assertEqual(embed.description, 'Tweet body in a box')
        self.assertEqual(embed.author.name, 'Bobby Skinner (@BobbySkinner_)')
        self.assertEqual(embed.author.icon_url, 'https://pbs.twimg.com/profile_images/example_400x400.jpg')
        self.assertEqual(embed.footer.text, 'Personal TweetCord')
        self.assertIsNone(embed.image.url)

    def test_video_candidates_only_use_mp4_formats(self):
        candidates = media_candidates({
            'type': 'video',
            'url': 'https://video.twimg.com/fallback.mp4',
            'formats': [
                {'container': 'm3u8', 'url': 'https://video.twimg.com/playlist.m3u8'},
                {'container': 'mp4', 'url': 'https://video.twimg.com/low.mp4', 'size': 3, 'bitrate': 256000},
                {'container': 'mp4', 'url': 'https://video.twimg.com/high.mp4', 'size': 8, 'bitrate': 2176000},
            ],
        }, 1)
        self.assertEqual([candidate.url for candidate in candidates], [
            'https://video.twimg.com/low.mp4',
            'https://video.twimg.com/high.mp4',
        ])
        ordered = _ordered_candidates(candidates, limit=10)
        self.assertEqual(ordered[0].url, 'https://video.twimg.com/high.mp4')

    def test_parsed_tweet_keeps_fx_media_and_retweet_sender(self):
        parsed = ParsedTweet({
            'tweet': {
                'raw_text': {'text': 'hello'},
                'author': {'screen_name': 'original', 'name': 'Original'},
                'reposted_by': {
                    'screen_name': 'DiscussingFilm',
                    'name': 'Discussing Film',
                    'avatar_url': 'https://pbs.twimg.com/profile_images/example_200x200.jpg',
                },
                'media': {
                    'all': [{
                        'type': 'video',
                        'url': 'https://video.twimg.com/fallback.mp4',
                        'thumbnail_url': 'https://pbs.twimg.com/thumb.jpg',
                        'formats': [{'container': 'mp4', 'url': 'https://video.twimg.com/video.mp4', 'size': 5}],
                    }],
                },
                'translation': {},
            },
        })
        self.assertEqual(parsed.sender_username, 'DiscussingFilm')
        self.assertEqual(parsed.media.items[0]['formats'][0]['url'], 'https://video.twimg.com/video.mp4')


class TestTweetDeliverySend(unittest.IsolatedAsyncioTestCase):
    async def test_none_view_is_omitted_from_regular_message(self):
        channel = SimpleNamespace(send=AsyncMock())
        delivery = TweetDelivery(SimpleNamespace())
        delivery._get_webhook = AsyncMock(return_value=None)

        await delivery.send(
            channel,
            content='<https://x.com/example/status/1>',
            username='Example | Personal TweetCord',
            avatar_url=None,
            view=None,
        )

        kwargs = channel.send.await_args.kwargs
        self.assertNotIn('view', kwargs)

    async def test_none_view_is_omitted_from_webhook_message(self):
        webhook = SimpleNamespace(send=AsyncMock())
        delivery = TweetDelivery(SimpleNamespace())
        delivery._get_webhook = AsyncMock(return_value=webhook)

        await delivery.send(
            SimpleNamespace(),
            content='<https://x.com/example/status/1>',
            username='Example | Personal TweetCord',
            avatar_url=None,
            view=None,
        )

        kwargs = webhook.send.await_args.kwargs
        self.assertNotIn('view', kwargs)

    async def test_custom_embed_remains_enabled(self):
        webhook = SimpleNamespace(send=AsyncMock())
        delivery = TweetDelivery(SimpleNamespace())
        delivery._get_webhook = AsyncMock(return_value=webhook)
        embed = SimpleNamespace()

        await delivery.send(
            SimpleNamespace(),
            content='<https://x.com/example/status/1>',
            username='Example | Personal TweetCord',
            avatar_url=None,
            embeds=[embed],
            suppress_embeds=False,
        )

        kwargs = webhook.send.await_args.kwargs
        self.assertEqual(kwargs['embeds'], [embed])
        self.assertFalse(kwargs['suppress_embeds'])


if __name__ == '__main__':
    unittest.main()
