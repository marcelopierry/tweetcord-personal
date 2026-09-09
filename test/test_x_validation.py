import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.notification.x_validation import validate_detail, TweetSuperseded
from src.notification.utils import TweetUnavailable, TweetRefreshError

OLD = '2096274968837288198'
NEW = '2096275016295792793'


def detail(tweet_id, ids):
    return {'data': {'entries': [{'entryId': 'tweet-' + tweet_id, 'content': {
        'itemContent': {'tweet_results': {'result': {
            'rest_id': tweet_id, 'edit_control': {'edit_tweet_ids': ids},
        }}}}}]}}


class TestXValidation(unittest.TestCase):
    def test_original_still_accessible_but_superseded(self):
        with self.assertRaises(TweetSuperseded):
            validate_detail(detail(OLD, [OLD, NEW]), OLD)

    def test_latest_edit_is_allowed(self):
        validate_detail(detail(NEW, [OLD, NEW]), NEW)

    def test_deleted_empty_result(self):
        with self.assertRaises(TweetUnavailable):
            validate_detail({'entryId': 'tweet-' + OLD, 'content': {
                'itemContent': {'tweet_results': {}}}}, OLD)

    def test_missing_or_incomplete_metadata_defers(self):
        for data in ({'errors': [{'code': 88}]}, detail(OLD, []), detail(NEW, [NEW])):
            with self.subTest(data=data), self.assertRaises(TweetRefreshError):
                validate_detail(data, OLD)

    def test_unedited_post_allowed(self):
        validate_detail(detail(OLD, [OLD]), OLD)

    def test_quoted_history_does_not_override_outer_history(self):
        data = detail(NEW, [NEW])
        data['quoted'] = detail(OLD, [OLD, NEW])
        validate_detail(data, NEW)


if __name__ == '__main__':
    unittest.main()
