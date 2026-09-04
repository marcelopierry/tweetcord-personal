import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.classes import ParsedTweet
from src.notification.delivery import ChannelDeliverySequencer, TweetDelivery, _ordered_candidates, build_delivery_links, build_delivery_text, build_quote_original_embed, build_tweet_embed, build_webhook_identity, extract_external_urls, extract_video_urls, extract_youtube_urls, get_delivery_references, media_candidates


class TestTweetDeliveryHelpers(unittest.TestCase):
    def test_quote_delivers_quote_then_original_link(self):
        parsed = SimpleNamespace(quote=SimpleNamespace(url='https://x.com/original/status/1'))
        links = build_delivery_links('https://x.com/tracked/status/2', parsed, is_quote=True)
        self.assertEqual(links, '<https://x.com/tracked/status/2>\n🔂 <https://x.com/original/status/1>')

    def test_retweet_delivers_wrapper_then_original_link(self):
        links = build_delivery_links(
            'https://x.com/tracked/status/2',
            None,
            is_quote=False,
            is_retweet=True,
            original_url='https://x.com/original/status/1',
        )
        self.assertEqual(
            links,
            '<https://x.com/tracked/status/2>\n🔄 <https://x.com/original/status/1>',
        )

    def test_delivery_text_uses_parsed_tweet_body(self):
        parsed = SimpleNamespace(get_text=lambda simplified_content: ('The tweet body', False))
        tweet = SimpleNamespace(text='fallback')
        self.assertEqual(build_delivery_text(tweet, parsed), 'The tweet body')

    def test_delivery_text_caps_long_fallback(self):
        tweet = SimpleNamespace(text='x' * 5000)
        self.assertLessEqual(len(build_delivery_text(tweet, None)), ParsedTweet.MAX_DESCRIPTION_LENGTH)

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

    def test_external_article_links_are_extracted_from_discord_markdown(self):
        text = (
            'Read [the full article](https://www.nytimes.com/athletic/7551353/story/) '
            'and visit https://example.com/news?team=giants.'
        )
        self.assertEqual(extract_external_urls(text), [
            'https://www.nytimes.com/athletic/7551353/story/',
            'https://example.com/news?team=giants',
        ])

    def test_external_links_are_deduplicated_and_x_links_are_ignored(self):
        text = (
            '[Article](https://example.com/story) https://example.com/story '
            '[@account](https://twitter.com/account) '
            'https://x.com/account/status/123'
        )
        self.assertEqual(extract_external_urls(text), ['https://example.com/story'])

    def test_each_external_link_keeps_its_first_appearance_order(self):
        text = 'https://first.example/a [Second](https://second.example/b) https://first.example/a'
        self.assertEqual(extract_external_urls(text), [
            'https://first.example/a',
            'https://second.example/b',
        ])

    def test_markdown_url_can_end_in_balanced_parentheses(self):
        text = '[Reference](https://example.com/wiki/Football_(American))'
        self.assertEqual(extract_external_urls(text), [
            'https://example.com/wiki/Football_(American)',
        ])

    def test_only_video_links_receive_separate_previews(self):
        text = (
            '[Article](https://example.com/news/story) '
            'https://youtu.be/video123 '
            'https://cdn.example.com/clip.mp4 '
            'https://vimeo.com/123456'
        )
        self.assertEqual(extract_video_urls(text), [
            'https://youtu.be/video123',
            'https://cdn.example.com/clip.mp4',
            'https://vimeo.com/123456',
        ])

    def test_retweet_claims_original_id_for_deduplication(self):
        original = SimpleNamespace(id='100', url='https://x.com/original/status/100')
        tweet = SimpleNamespace(id='200', url='https://x.com/tracked/status/200', is_retweet=True, is_quoted=False, retweeted_tweet=original)
        references = get_delivery_references(tweet, None)
        self.assertEqual(references.claim_id, 'original:100')
        self.assertEqual(references.tweet_id, '200')
        self.assertEqual(references.record_ids, ('post:200', 'original:100'))

    def test_retweet_recovers_original_reference_from_fx_when_tweety_omits_it(self):
        tweet = SimpleNamespace(
            id='200',
            url='https://x.com/retweeter/status/200',
            is_retweet=True,
            is_quoted=False,
            retweeted_tweet=None,
        )
        parsed = SimpleNamespace(
            source_id='100',
            source_url='https://x.com/original/status/100',
        )
        references = get_delivery_references(tweet, parsed)
        self.assertEqual(references.claim_id, 'original:100')
        self.assertEqual(references.original_url, parsed.source_url)
        self.assertEqual(references.record_ids, ('post:200', 'original:100'))
        self.assertEqual(
            build_delivery_links(
                tweet.url,
                parsed,
                is_quote=False,
                is_retweet=True,
                original_url=references.original_url,
            ),
            '<https://x.com/retweeter/status/200>\n🔄 <https://x.com/original/status/100>',
        )

    def test_quote_claims_its_own_id_even_when_original_was_seen(self):
        original = SimpleNamespace(id='100', url='https://x.com/original/status/100')
        tweet = SimpleNamespace(id='200', url='https://x.com/tracked/status/200', is_retweet=False, is_quoted=True, quoted_tweet=original)
        references = get_delivery_references(tweet, None)
        self.assertEqual(references.claim_id, 'post:200')
        self.assertEqual(references.original_id, '100')
        self.assertEqual(references.record_ids, ('post:200',))

    def test_direct_original_and_retweet_share_the_same_dedupe_key(self):
        direct = SimpleNamespace(
            id='100', url='https://x.com/original/status/100',
            is_retweet=False, is_quoted=False, quoted_tweet=None,
        )
        retweet = SimpleNamespace(
            id='200', url='https://x.com/tracked/status/200',
            is_retweet=True, is_quoted=False,
            retweeted_tweet=SimpleNamespace(id='100', url=direct.url),
        )
        self.assertEqual(
            get_delivery_references(direct, None).claim_id,
            get_delivery_references(retweet, None).claim_id,
        )

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

    def test_retweet_card_uses_original_author_while_webhook_uses_retweeter(self):
        original_author = SimpleNamespace(
            name='Charlotte Carroll',
            username='charlottecrrll',
            profile_image_url_https='https://pbs.twimg.com/original_normal.jpg',
        )
        original = SimpleNamespace(author=original_author, created_on=None)
        tweet = SimpleNamespace(
            is_retweet=True,
            retweeted_tweet=original,
            author=SimpleNamespace(name='Dan Duggan', username='DDuggan21'),
        )
        parsed = SimpleNamespace(
            sender_name='Dan Duggan',
            sender_username='DDuggan21',
            sender_avatar_url='https://pbs.twimg.com/retweeter_normal.jpg',
            author_name='Charlotte Carroll',
            author_username='charlottecrrll',
            author_avatar_url='https://pbs.twimg.com/original_normal.jpg',
        )

        webhook_name, _ = build_webhook_identity('DDuggan21', tweet, parsed)
        embed = build_tweet_embed('DDuggan21', tweet, parsed, 'Original tweet text')

        self.assertEqual(webhook_name, 'Dan Duggan | Personal TweetCord')
        self.assertEqual(embed.author.name, 'Charlotte Carroll (@charlottecrrll)')
        self.assertEqual(embed.description, 'Original tweet text')

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

    def test_media_filename_prefix_distinguishes_quote_and_original(self):
        quote = media_candidates({'type': 'photo', 'url': 'https://pbs.twimg.com/quote.jpg'}, 1, 'quote')
        original = media_candidates({'type': 'photo', 'url': 'https://pbs.twimg.com/original.jpg'}, 1, 'original')
        self.assertEqual(quote[0].filename, 'quote-photo-1.jpg')
        self.assertEqual(original[0].filename, 'original-photo-1.jpg')

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
        self.assertEqual(parsed.author_username, 'original')
        self.assertEqual(parsed.text, 'hello')
        self.assertEqual(parsed.media.items[0]['formats'][0]['url'], 'https://video.twimg.com/video.mp4')


class TestTweetDeliverySend(unittest.IsolatedAsyncioTestCase):
    async def test_channel_sequencer_keeps_multipart_deliveries_together(self):
        sequencer = ChannelDeliverySequencer()
        first_started = asyncio.Event()
        events = []

        async def first():
            async with sequencer.lock_for(123):
                events.append('first-link')
                first_started.set()
                await asyncio.sleep(0)
                events.append('first-media')

        async def second():
            await first_started.wait()
            async with sequencer.lock_for(123):
                events.append('second-link')
                events.append('second-media')

        await asyncio.gather(first(), second())
        self.assertEqual(events, [
            'first-link',
            'first-media',
            'second-link',
            'second-media',
        ])

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
        self.assertTrue(kwargs['wait'])

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
