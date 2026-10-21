import unittest
from types import SimpleNamespace as NS

from core.classes import ParsedTweet
from src.notification.delivery import (
    build_delivery_text, build_quote_original_embed, build_tweet_embed,
    extract_external_urls, extract_video_urls,
)


class FullUrlTests(unittest.TestCase):
    ARTICLE = 'https://discussingfilm.net/2026/10/21/clayface-review-dcu-gruesome-campy/'

    def raw(self, url=None, display=None, prefix='Read more: '):
        token = 'https://t.co/I3hf0osFUI'
        return {'text': prefix+token, 'facets': [{
            'type': 'url', 'indices': [len(prefix), len(prefix)+len(token)],
            'original': token, 'replacement': url or self.ARTICLE,
            'display': display or 'discussingfilm.net/2026/10/21/cla…',
        }]}

    def test_exact_reported_tweet_keeps_entire_expanded_article_url(self):
        prefix = ('‘CLAYFACE’ works best when it leans into gruesome body horror and campy horror fun.'
                  '\n\nFind out more in our review... ')
        parsed = ParsedTweet({'tweet': {'raw_text': self.raw(prefix=prefix)}})
        text = build_delivery_text(NS(), parsed)
        self.assertEqual(text, prefix+self.ARTICLE)
        self.assertNotIn('cla…', text)
        self.assertNotIn('t.co/', text)
        self.assertEqual(extract_external_urls(text), [self.ARTICLE])
        self.assertEqual(extract_video_urls(text), [])

    def test_normal_and_retweet_cards_show_the_full_url(self):
        parsed = ParsedTweet({'tweet': {'raw_text': self.raw(),
                            'author': {'name':'DiscussingFilm','screen_name':'DiscussingFilm'}}})
        for is_retweet in (False, True):
            tweet = NS(is_retweet=is_retweet, author=NS(name='DiscussingFilm',username='DiscussingFilm'))
            embed = build_tweet_embed('DiscussingFilm', tweet, parsed, build_delivery_text(tweet, parsed))
            self.assertEqual(embed.description, 'Read more: '+self.ARTICLE)

    def test_quoted_original_has_full_url_too(self):
        parsed = ParsedTweet({'tweet': {'raw_text': {'text':'My thoughts'},
                            'quote': {'raw_text':self.raw(), 'author':{'name':'Author','screen_name':'author'}}}})
        embed = build_quote_original_embed(NS(is_quoted=True, quoted_tweet=None), parsed)
        self.assertEqual(embed.description, 'Read more: '+self.ARTICLE)

    def test_video_links_still_receive_separate_preview(self):
        url = 'https://www.youtube.com/watch?v=abc123&feature=share'
        text = ParsedTweet._handle_raw_text(self.raw(url, 'youtube.com/watch?v=abc…'))
        self.assertIn(url, text)
        self.assertEqual(extract_video_urls(text), [url])

    def test_query_entities_unicode_and_stale_offsets_are_preserved(self):
        raw = self.raw('https://example.com/story?q=one&amp;page=two#details', prefix='🎬 Logic &amp; links: ')
        raw['facets'][0]['indices'] = [999, 1022]
        self.assertEqual(ParsedTweet._handle_raw_text(raw),
                         '🎬 Logic & links: https://example.com/story?q=one&page=two#details')

    def test_missing_expansion_keeps_original_link_instead_of_short_label(self):
        raw = self.raw()
        raw['facets'][0]['replacement'] = None
        self.assertEqual(ParsedTweet._handle_raw_text(raw), 'Read more: https://t.co/I3hf0osFUI')

    def test_media_tokens_are_removed_but_article_link_is_retained(self):
        raw = self.raw()
        token = 'https://t.co/photo'
        start = len(raw['text'])+1
        raw['text'] += ' '+token
        raw['facets'].append({'type':'media', 'indices':[start,start+len(token)], 'original':token})
        self.assertEqual(ParsedTweet._handle_raw_text(raw), 'Read more: '+self.ARTICLE+' ')
