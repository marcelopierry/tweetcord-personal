import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.classes import ParsedTweet
from src.notification.delivery import _ordered_candidates, build_delivery_links, build_webhook_identity, media_candidates


class TestTweetDeliveryHelpers(unittest.TestCase):
    def test_quote_delivers_original_then_quote_link(self):
        parsed = SimpleNamespace(quote=SimpleNamespace(url='https://x.com/original/status/1'))
        links = build_delivery_links('https://x.com/tracked/status/2', parsed, is_quote=True)
        self.assertEqual(links, '<https://x.com/original/status/1>\n<https://x.com/tracked/status/2>')

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


if __name__ == '__main__':
    unittest.main()
