import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from src.notification.channel_filter import GIANTS_CHANNEL_FILTER as FILTER, normalize, player_terms


class TestGiantsChannelFilter(unittest.TestCase):
    def reason(self, text, quote='', channel=None, account='reporter'):
        parsed = NS(text=text, quote=NS(text=quote))
        return FILTER.blocked_reason(channel or FILTER.channel_id, account, NS(), parsed)

    def test_requested_examples_and_overrides(self):
        self.assertIsNotNone(self.reason("Let's go Knicks"))
        self.assertIsNone(self.reason('Malik Nabers and Jaxon Dart went to the Knicks game last night'))
        for term in ('Giants', 'NYG', 'New York Giants', 'John Harbaugh', 'Harbaugh',
                     'NFL', 'Dart', 'Nabers', 'Abdul Carter', 'Brian Burns', 'Cam Skattebo'):
            with self.subTest(term=term):
                self.assertIsNone(self.reason(f'{term} watched the Yankees'))

    def test_every_roster_player_full_first_and_last_name(self):
        import json
        from pathlib import Path
        rules = json.loads(Path('src/notification/giants_filter_rules.json').read_text())
        for team, names in rules['blocked_players'].items():
            for term in player_terms(names):
                with self.subTest(team=team, term=term):
                    # Overrides always win, including any shared names.
                    if not FILTER.allowed.search(normalize(term)):
                        self.assertIsNotNone(self.reason(f'{term} is playing tonight'))

    def test_all_team_names_nicknames_accents_and_case(self):
        for text in ('YANKEES!', '#Mets', 'Knicks', 'Nets', 'Rangers', 'Islanders', 'Devils',
                     'Deuce scored', 'KAT scored', 'The Martian homered', '#AllRise',
                     'José Caballero', 'Jose Caballero', 'J.C. Escarra', 'JC Escarra'):
            with self.subTest(text=text):
                self.assertIsNotNone(self.reason(text))

    def test_scope_and_official_account(self):
        self.assertIsNone(self.reason('Knicks', channel='1543703698268495922'))
        self.assertIsNone(self.reason('Knicks', account='Giants'))
        self.assertIsNone(self.reason('Knicks', account='@GIANTS'))
        self.assertIsNotNone(self.reason('Knicks', account='GiantsReporter'))

    def test_quoted_original_can_block_or_allow(self):
        self.assertIsNotNone(self.reason('Great game', quote='Knicks win'))
        self.assertIsNone(self.reason('Knicks win', quote='Malik Nabers attended'))
        self.assertIsNone(self.reason('Giants player attended', quote='Knicks win'))

    def test_word_boundaries_urls_and_fresh_text(self):
        self.assertIsNone(self.reason('The internet works'))
        self.assertIsNone(self.reason('Lets play https://example.com/knicks'))
        self.assertIsNotNone(self.reason('Knicks https://example.com/giants'))
        self.assertIsNotNone(self.reason('[Knicks](https://example.com/giants)'))
        self.assertIsNone(FILTER.blocked_reason(FILTER.channel_id, 'reporter',
            NS(text='Knicks'), NS(text='Giants update', quote=NS(text=''))))
        self.assertIsNotNone(FILTER.blocked_reason(FILTER.channel_id, 'reporter',
            NS(text='Giants'), NS(text='Knicks update', quote=NS(text=''))))

    def test_raw_retweet_and_quote_fallback(self):
        tweet = NS(text='', retweeted_tweet=NS(text='Knicks win', quoted_tweet=NS(text='')))
        self.assertIsNotNone(FILTER.blocked_reason(FILTER.channel_id, 'reporter', tweet))


class TestFilterDeliveryBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_post_sends_nothing_and_does_not_claim_history(self):
        from src.notification.account_tracker import AccountTracker
        tracker = AccountTracker.__new__(AccountTracker)
        tracker.delivery_history = NS(claim=AsyncMock())
        tracker.delivery = NS(send=AsyncMock())
        await tracker._deliver_tweet_to_channel('reporter', NS(id='123'), {},
            NS(id=int(FILTER.channel_id)), NS(text='Knicks win', quote=NS(text='')), None, None)
        tracker.delivery_history.claim.assert_not_awaited()
        tracker.delivery.send.assert_not_awaited()
