import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.account_tracker import AccountTracker
from src.notification.utils import TweetRefreshError, TweetUnavailable, fetch_fresh_parsed_tweet


class FakeResponse:
    def __init__(self, status, data=None):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)

    def get(self, url):
        return self.responses.pop(0)


def tweet_payload(text='edited text'):
    return {
        'tweet': {
            'id': '100',
            'url': 'https://x.com/example/status/100',
            'raw_text': {'text': text, 'facets': []},
            'author': {'name': 'Example', 'screen_name': 'example'},
            'media': {'all': []},
            'translation': {},
        },
    }


class TestFreshTweetFetch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tweet = SimpleNamespace(
            id='100',
            url='https://x.com/example/status/100',
        )

    async def test_available_tweet_returns_fresh_edited_representation(self):
        parsed = await fetch_fresh_parsed_tweet(
            self.tweet,
            FakeSession(FakeResponse(200, tweet_payload('edited version'))),
        )
        self.assertEqual(parsed.text, 'edited version')

    async def test_deleted_tweet_raises_unavailable_without_stale_fallback(self):
        with self.assertRaises(TweetUnavailable):
            await fetch_fresh_parsed_tweet(
                self.tweet,
                FakeSession(FakeResponse(404, {'tweet': None})),
            )

    async def test_two_unavailable_checks_cancel_instead_of_deliver(self):
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.session = FakeSession(FakeResponse(404), FakeResponse(404))
        tracker.unavailable_checks = {}
        pending = SimpleNamespace(defer=Mock())

        first_status, _ = await tracker._refresh_ready_tweet(
            pending, self.tweet, 'example', 'client',
        )
        second_status, _ = await tracker._refresh_ready_tweet(
            pending, self.tweet, 'example', 'client',
        )

        self.assertEqual(first_status, 'deferred')
        self.assertEqual(second_status, 'deleted')
        pending.defer.assert_called_once_with(self.tweet, retry_seconds=60)
        self.assertEqual(tracker.unavailable_checks, {})

    async def test_temporary_refresh_failure_defers_without_marking_deleted(self):
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.session = FakeSession(FakeResponse(503))
        tracker.unavailable_checks = {}
        pending = SimpleNamespace(defer=Mock())

        status, parsed = await tracker._refresh_ready_tweet(
            pending, self.tweet, 'example', 'client',
        )

        self.assertEqual(status, 'deferred')
        self.assertIsNone(parsed)
        pending.defer.assert_called_once_with(self.tweet, retry_seconds=60)
        self.assertEqual(tracker.unavailable_checks, {})


if __name__ == '__main__':
    unittest.main()
