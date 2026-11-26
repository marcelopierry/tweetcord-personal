import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from core.classes import ParsedTweet
from src.notification.account_tracker import AccountTracker
from src.notification.spoiler_blocks import SpoilerBlocks
from src.notification.delivery import (
    MediaDelivery, build_delivery_text, build_media_caption, build_tweet_embed,
)


def media_post(post_id, handle, name, kind, token):
    url = f'https://x.com/{handle}/status/{post_id}'
    return {
        'id': post_id, 'url': url,
        'author': {'name': name, 'screen_name': handle},
        'raw_text': {'text': token, 'facets': [
            {'type': 'media', 'original': token, 'indices': [0, len(token)]},
        ]},
        'media': {'all': [{'type': kind}]},
    }


def notification(post_id, handle, name, text, **kwargs):
    return NS(
        id=post_id, url=f'https://twitter.com/{handle}/status/{post_id}',
        author=NS(name=name, username=handle), text=text,
        is_retweet=kwargs.pop('is_retweet', False),
        is_quoted=kwargs.pop('is_quoted', False), **kwargs,
    )


class MediaOnlyTextTests(unittest.TestCase):
    def test_standalone_photo_or_video_gets_an_attributed_card_without_stale_token(self):
        token = 'https://t.co/media'
        for kind in ('photo', 'video'):
            with self.subTest(kind=kind):
                tweet = notification('2120746905705812235', 'chargers', 'Los Angeles Chargers', token)
                parsed = ParsedTweet({'tweet': media_post(tweet.id, 'chargers', tweet.author.name, kind, token)})
                body = build_delivery_text(tweet, parsed)
                self.assertEqual(body, '')
                card = build_tweet_embed('chargers', tweet, parsed, body)
                self.assertEqual(card.description, '\u200b')
                self.assertIn('@chargers', card.author.name)
                self.assertIsNotNone(card.timestamp)

    def test_explicitly_empty_caption_does_not_use_stale_wrapper_text(self):
        parsed = ParsedTweet({'tweet': {'raw_text': {'text': ''}}})
        self.assertEqual(build_delivery_text(NS(text='stale'), parsed), '')

    def test_retweet_without_parsed_text_uses_only_the_original_body(self):
        tweet = NS(is_retweet=True, text='RT @example: stale wrapper', retweeted_tweet=None)
        self.assertEqual(build_delivery_text(tweet, None), '')
        tweet.retweeted_tweet = NS(text='Original caption https://example.com/full-link')
        self.assertEqual(build_delivery_text(tweet, None), tweet.retweeted_tweet.text)

    def test_media_caption_escapes_discord_markup_and_mentions_in_names(self):
        tweet = notification('123', 'example', 'Name_* @everyone', '')
        parsed = ParsedTweet({'tweet': media_post(tweet.id, 'example', tweet.author.name, 'video', '')})
        self.assertEqual(
            build_media_caption('example', tweet, parsed, quoted=False),
            r'▷ *Video from Name\_\* @' + '\u200b' + 'everyone*',
        )


class MediaOnlyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, tweet, parsed, primary=None, original=None):
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.spoiler_blocks = SpoilerBlocks(':memory:')
        tracker.session = NS()
        tracker.delivery = NS(send=AsyncMock())
        tracker.delivery_history = NS(claim=AsyncMock(return_value=True),
                                      release=AsyncMock(), record=AsyncMock())
        channel = NS(id=321, mention='<#321>', guild=NS(id=111, filesize_limit=25_000_000))
        with patch('src.notification.account_tracker.EMBED_TYPE', 'built_in'), patch(
            'src.notification.account_tracker.prepare_media_delivery', new=AsyncMock(side_effect=[
                MediaDelivery(files=[primary] if primary else [], fallback_urls=[]),
                MediaDelivery(files=[original] if original else [], fallback_urls=[]),
            ]),
        ):
            await tracker._deliver_tweet_to_channel(
                tweet.author.username, tweet, {'role_id': None, 'customized_msg': None},
                channel, parsed, None, None,
            )
        tracker.delivery_history.release.assert_not_awaited()
        return [call.kwargs for call in tracker.delivery.send.await_args_list]

    async def test_chargers_media_only_quote_has_two_cards_then_photo(self):
        token = 'https://t.co/QzaZ40ZFF7'
        original = media_post('2120746905705812235', 'chargers', 'Los Angeles Chargers', 'photo', token)
        tweet = notification('2120753981127590303', 'elitetakes_', 'Nico', 'Wait what’s going on?!',
                             is_quoted=True, quoted_tweet=NS(id=original['id'], url=original['url'], text=token))
        parsed = ParsedTweet({'tweet': {'raw_text': {'text': tweet.text}, 'quote': original}})
        attachment = object()
        main, media = await self.deliver(tweet, parsed, original=attachment)
        self.assertEqual(main['content'], f'<{tweet.url}>\n🔂 <{original["url"]}>')
        self.assertEqual([card.description for card in main['embeds']], [tweet.text, '\u200b'])
        self.assertEqual(main['embeds'][1].author.name, 'Los Angeles Chargers (@chargers)')
        self.assertIsNotNone(main['embeds'][1].timestamp)
        self.assertEqual(media['content'], '▷ *Photo from Los Angeles Chargers*')
        self.assertEqual(media['files'], [attachment])

    async def test_josh_downs_gif_repost_has_no_wrapper_text_and_keeps_media(self):
        token = 'https://t.co/SWGjirDe9N'
        original = media_post('2120255367984656516', 'JoshDowns', 'JD', 'gif', token)
        tweet = notification('2120257176643977422', 'RapSheet', 'Ian Rapoport', f'RT @JoshDowns: {token}',
                             is_retweet=True, retweeted_tweet=None)
        parsed = ParsedTweet({'tweet': original})
        attachment = object()
        main, media = await self.deliver(tweet, parsed, primary=attachment)
        self.assertEqual(main['content'], f'<{tweet.url}>\n🔄 <{original["url"]}>')
        self.assertEqual(main['username'], 'Ian Rapoport | Free Twitter Bot')
        self.assertEqual(len(main['embeds']), 1)
        card = main['embeds'][0]
        self.assertEqual(card.author.name, 'JD (@JoshDowns)')
        self.assertEqual(card.description, '\u200b')
        self.assertIsNotNone(card.timestamp)
        self.assertEqual(media['content'], '▷ *GIF from JD*')
        self.assertEqual(media['files'], [attachment])

    async def test_self_quote_labels_each_media_group_with_its_source_post(self):
        token = 'https://t.co/media'
        tweet = notification('2120753981127590303', 'example', 'Example', token, is_quoted=True)
        new_post = media_post(tweet.id, 'example', 'Example', 'video', token)
        new_post['quote'] = media_post('2120746905705812235', 'Example', 'Example', 'photo', token)
        parsed = ParsedTweet({'tweet': new_post})

        main, new_media, original_media = await self.deliver(
            tweet, parsed, primary=object(), original=object(),
        )
        self.assertEqual([card.description for card in main['embeds']], ['\u200b', '\u200b'])
        self.assertEqual(new_media['content'], '▷ *Video from Example (new tweet)*')
        self.assertEqual(original_media['content'], '▷ *Photo from Example (original tweet)*')
