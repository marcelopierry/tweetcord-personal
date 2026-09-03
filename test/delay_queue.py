git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-nkjEQ83n' (errno=Operation not permitted)
2026-09-02 22:51:06.638 xcodebuild[56277:5959503]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:06.764 xcodebuild[56277:5959501] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-4uyq0DpO' (errno=Operation not permitted)
2026-09-02 22:51:07.151 xcodebuild[56281:5959517]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:07.277 xcodebuild[56281:5959516] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.delay_queue import DelayedTweetBuffer


class FakeTweet:
    def __init__(self, tweet_id, created_on, text):
        self.id = tweet_id
        self.created_on = created_on
        self.text = text


class TestDelayedTweetBuffer(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.buffer = DelayedTweetBuffer(300)

    def test_waits_until_due(self):
        tweet = FakeTweet('1', self.start, 'hello')
        self.buffer.add(tweet, self.start)
        self.assertEqual(self.buffer.pop_ready(self.start + timedelta(seconds=299)), [])
        self.assertEqual(self.buffer.pop_ready(self.start + timedelta(seconds=300)), [tweet])

    def test_refreshes_object_without_resetting_delay(self):
        original = FakeTweet('1', self.start, 'before edit')
        edited = FakeTweet('1', self.start, 'after edit')
        self.buffer.add(original, self.start)
        self.buffer.add(edited, self.start + timedelta(seconds=30))
        released = self.buffer.pop_ready(self.start + timedelta(seconds=300))
        self.assertEqual(released[0].text, 'after edit')

    def test_deduplicates_by_id(self):
        self.buffer.add(FakeTweet('1', self.start, 'first'), self.start)
        self.buffer.add(FakeTweet('1', self.start, 'same tweet'), self.start)
        self.assertEqual(len(self.buffer), 1)

    def test_delay_change_updates_queued_tweets_retroactively(self):
        tweet = FakeTweet('1', self.start, 'hello')
        self.buffer.add(tweet, self.start)
        self.buffer.set_delay_seconds(180)
        self.assertEqual(self.buffer.pop_ready(self.start + timedelta(seconds=179)), [])
        self.assertEqual(self.buffer.pop_ready(self.start + timedelta(seconds=180)), [tweet])

    def test_validation_retry_uses_short_defer_without_full_delay(self):
        tweet = FakeTweet('1', self.start, 'hello')
        self.buffer.add(tweet, self.start)
        self.assertEqual(self.buffer.pop_ready(self.start + timedelta(seconds=300)), [tweet])

        retry_at = self.start + timedelta(seconds=300)
        self.buffer.defer(tweet, retry_seconds=60, now=retry_at)
        self.assertEqual(self.buffer.pop_ready(retry_at + timedelta(seconds=59)), [])
        self.assertEqual(self.buffer.pop_ready(retry_at + timedelta(seconds=60)), [tweet])


if __name__ == '__main__':
    unittest.main()
