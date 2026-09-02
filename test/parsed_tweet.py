import os
import sys
import unittest
from unittest.mock import patch

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.classes import ParsedTweet

class MockTweet:
    def __init__(self, media=None):
        self.media = media or []

class TestParsedTweet(unittest.TestCase):
    def setUp(self):
        # Intercept and simulate the translation function t() to prevent a missing key error from being thrown
        self.patcher = patch('core.classes.t', side_effect=lambda key, **kwargs: f"Mock({key})")
        self.mock_t = self.patcher.start()
        
        # Create a basic ParsedTweet instance using a dict
        self.source_dict = {
            'tweet': {
                'raw_text': {'text': None},
                'author': {'screen_name': 'test_user'},
                'media': {'all': []},
                'translation': {'text': None, 'source_lang': 'en'}
            }
        }
        self.parsed_tweet = ParsedTweet(self.source_dict)
        
    def tearDown(self):
        # Stop interception after the test ends
        self.patcher.stop()

    def test_get_text_priority(self):
        """Test that get_text prioritizes translated text over raw text."""
        self.parsed_tweet.text = "Original Text"
        self.parsed_tweet.trans_text = "Translated Text"
        self.parsed_tweet.trans_lang = "en"
        
        # Should return translated text (which is a formatted string in get_translated_text)
        result, is_simplified = self.parsed_tweet.get_text()
        self.assertIn("Translated Text", result)
        # Confirmed call to Mock translation
        self.assertIn("Mock(class.parsed_tweet.trans_text)", result) 
        self.assertFalse(is_simplified)
        
        # Should return original text if translation is missing
        self.parsed_tweet.trans_text = None
        result, is_simplified = self.parsed_tweet.get_text()
        self.assertEqual(result, "Original Text")
        self.assertFalse(is_simplified)

    def test_get_text_none(self):
        """Test get_text when both text and trans_text are None."""
        self.parsed_tweet.text = None
        self.parsed_tweet.trans_text = None
        self.assertIsNone(self.parsed_tweet.get_text())

    def test_simplified_content_threshold(self):
        """Test that _simplified_content correctly identifies content over threshold."""
        # SIMPLIFIED_THRESHOLD is 400
        # MAX_DESCRIPTION_LENGTH follows Discord's embed description limit.
        short_text = "A" * 100
        long_text = "A" * 4500
        
        # Short text should not be simplified
        result, is_simplified = self.parsed_tweet._simplified_content(short_text)
        self.assertEqual(result, short_text)
        self.assertFalse(is_simplified)
        
        # Long text should be simplified (is_simplified becomes True if > 400)
        result, is_simplified = self.parsed_tweet._simplified_content(long_text)
        self.assertTrue(is_simplified)
        self.assertTrue(len(result) < len(long_text))
        self.assertTrue(result.endswith("..."))

    def test_get_text_simplified(self):
        """Test get_text with simplified_content=True."""
        long_text = "A" * 4500
        self.parsed_tweet.text = long_text
        
        result, is_simplified = self.parsed_tweet.get_text(simplified_content=True)
        self.assertTrue(is_simplified)
        self.assertTrue(len(result) < 4500)

    def test_retweet_text_does_not_add_a_synthetic_rt_prefix(self):
        """Retweet cards display the original text without an RT wrapper."""
        rt_source_dict = {
            'tweet': {
                'raw_text': {'text': 'Original Text'},
                'author': {'screen_name': 'original_author'},
                'reposted_by': {'screen_name': 'retweeter'},
                'media': {'all': []},
                'translation': {'text': 'Translated Text', 'source_lang': 'en'}
            }
        }
        rt_parsed_tweet = ParsedTweet(rt_source_dict)
        
        self.assertEqual(rt_parsed_tweet.text, "Original Text")
        self.assertEqual(rt_parsed_tweet.trans_text, "Translated Text")

        result, _ = rt_parsed_tweet.get_text()
        self.assertNotIn("RT [@original_author]", result)
        self.assertIn("Translated Text", result)

    def test_get_quote_text_repro(self):
        """Test get_quote_text with the new simplified logic."""
        self.parsed_tweet.quote.text = "Quote Content"
        self.parsed_tweet.text = "Main Content"
        
        # Test with main text included
        result = self.parsed_tweet.get_quote_text(include_main_text=True)
        # result should be (content, is_simplified)
        self.assertIsInstance(result, tuple)
        content, _ = result
        self.assertIn("Main Content", content)
        self.assertIn("> Quote Content", content)
        self.assertNotIn("('Main Content', False)", content)

    def test_quote_author_avatar_is_retained(self):
        parsed = ParsedTweet({
            'tweet': {
                'raw_text': {'text': 'Commentary'},
                'author': {'screen_name': 'quoter'},
                'quote': {
                    'raw_text': {'text': 'Original'},
                    'author': {
                        'screen_name': 'original',
                        'name': 'Original Author',
                        'avatar_url': 'https://pbs.twimg.com/original.jpg',
                    },
                },
                'media': {'all': []},
                'translation': {},
            },
        })
        self.assertEqual(parsed.quote.avatar_url, 'https://pbs.twimg.com/original.jpg')

    def test_quote_and_original_media_are_kept_separate(self):
        parsed = ParsedTweet({
            'tweet': {
                'raw_text': {'text': 'Commentary'},
                'author': {'screen_name': 'quoter'},
                'media': {
                    'all': [{'type': 'photo', 'url': 'https://pbs.twimg.com/quote.jpg'}],
                },
                'quote': {
                    'raw_text': {'text': 'Original'},
                    'author': {'screen_name': 'original'},
                    'media': {
                        'all': [{
                            'type': 'video',
                            'url': 'https://video.twimg.com/original.mp4',
                            'thumbnail_url': 'https://pbs.twimg.com/original-thumb.jpg',
                        }],
                    },
                },
                'translation': {},
            },
        })
        self.assertEqual(parsed.media.items[0]['url'], 'https://pbs.twimg.com/quote.jpg')
        self.assertEqual(parsed.quote_media.items[0]['url'], 'https://video.twimg.com/original.mp4')
        self.assertEqual(parsed.length, 2)

    def test_quote_only_media_does_not_get_misclassified_as_main_media(self):
        parsed = ParsedTweet({
            'tweet': {
                'raw_text': {'text': 'Commentary'},
                'author': {'screen_name': 'quoter'},
                'media': {'all': []},
                'quote': {
                    'raw_text': {'text': 'Original'},
                    'author': {'screen_name': 'original'},
                    'media': {'all': [{'type': 'photo', 'url': 'https://pbs.twimg.com/original.jpg'}]},
                },
                'translation': {},
            },
        })
        self.assertEqual(parsed.media.items, [])
        self.assertEqual(parsed.quote_media.items[0]['url'], 'https://pbs.twimg.com/original.jpg')

    def test_markdown_escaping(self):
        """Test that ParsedTweet escapes markdown in source text."""
        source = {
            'tweet': {
                'raw_text': {'text': '(*’▽’) #SDVX'},
                'author': {'screen_name': 'test_user'},
                'media': {'all': []},
                'translation': {'text': None, 'source_lang': 'ja'}
            }
        }
        parsed = ParsedTweet(source)
        # Check text (should be escaped)
        self.assertEqual(parsed.text, r"\(\*’▽’\) #SDVX")
        
        # Check that get_text returns escaped text
        text, _ = parsed.get_text()
        self.assertEqual(text, r"\(\*’▽’\) #SDVX")

    def test_duplicate_facets_in_raw_text(self):
        """Test that duplicate facets are correctly formatted when indices are provided."""
        source = {
            'tweet': {
                'raw_text': {
                    'text': 'Abstract #TAG1 and #TAG2 and #TAG1.',
                    'facets': [
                        {"type": "hashtag", "indices": [9, 14], "original": "TAG1"},
                        {"type": "hashtag", "indices": [19, 24], "original": "TAG2"},
                        {"type": "hashtag", "indices": [29, 34], "original": "TAG1"}
                    ]
                },
                'author': {'screen_name': 'test_user'},
                'media': {'all': []},
                'translation': {'text': None, 'source_lang': 'en'}
            }
        }
        parsed = ParsedTweet(source)
        
        expected_text = (
            r"Abstract [#TAG1](https://twitter.com/hashtag/TAG1) and [#TAG2](https://twitter.com/hashtag/TAG2) and [#TAG1](https://twitter.com/hashtag/TAG1)."
        )
        self.assertEqual(parsed.text, expected_text)

    def test_stale_media_facet_does_not_delete_valid_note_tweet_text(self):
        text = (
            'First details on the weapon system for GTA 6:\n\n'
            '• 4 weapons will be available in your inventory - including 2 long-range, 1 pistol & 1 melee\n\n'
            '• Other weapons can be stored in your car trunk or hideout\n\n'
            '• NPCs will immediately notice if you have your weapon drawn\n\n'
            '• In-depth weapon customization'
        )
        parsed = ParsedTweet({
            'tweet': {
                'id': '2094901992191672488',
                'url': 'https://x.com/DiscussingFilm/status/2094901992191672488',
                'raw_text': {
                    'text': text,
                    'facets': [{
                        'type': 'media',
                        'indices': [278, 301],
                        'original': 'https://t.co/jwHWeK2Xtp',
                    }],
                },
                'author': {'screen_name': 'DiscussingFilm'},
                'media': {'all': []},
                'translation': {},
            },
        })
        self.assertEqual(parsed.text, text)
        self.assertTrue(parsed.text.endswith('weapon customization'))

    def test_html_entities_are_decoded_before_url_facets(self):
        parsed = ParsedTweet({
            'tweet': {
                'raw_text': {
                    'text': 'Logic &amp; good arguments. Source: https://t.co/story',
                    'facets': [{
                        'type': 'url',
                        'indices': [32, 50],
                        'original': 'https://t.co/story',
                        'display': 'example.com/story',
                        'replacement': 'https://example.com/story',
                    }],
                },
                'author': {'screen_name': 'example'},
                'media': {'all': []},
                'translation': {},
            },
        })
        self.assertEqual(
            parsed.text,
            'Logic & good arguments. Source: [example.com/story](https://example.com/story)',
        )

    def test_fx_canonical_source_reference_is_retained_for_retweets(self):
        parsed = ParsedTweet({
            'tweet': {
                'id': '100',
                'url': 'https://x.com/original/status/100',
                'raw_text': {'text': 'Original text'},
                'author': {'screen_name': 'original'},
                'reposted_by': {'screen_name': 'retweeter'},
                'media': {'all': []},
                'translation': {},
            },
        })
        self.assertEqual(parsed.source_id, '100')
        self.assertEqual(parsed.source_url, 'https://x.com/original/status/100')

if __name__ == '__main__':
    unittest.main()
