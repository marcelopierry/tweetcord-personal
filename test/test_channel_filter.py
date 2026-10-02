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

    def test_every_roster_player_full_name(self):
        import json
        from pathlib import Path
        rules = json.loads(Path('src/notification/giants_filter_rules.json').read_text())
        for team, names in rules['blocked_players'].items():
            for term in player_terms(names):
                with self.subTest(team=team, term=term):
                    # Overrides always win, including any shared names.
                    if not FILTER.allowed.search(normalize(term)):
                        self.assertIsNotNone(self.reason(f'{term} is playing tonight'))

    def test_all_team_names_contextual_nicknames_accents_and_case(self):
        for text in ('YANKEES!', '#Mets', 'Knicks', 'Nets', 'Rangers', 'Islanders', 'Devils',
                     'Deuce dunked', 'KAT hit a three pointer', 'The Martian homered', '#AllRise homered',
                     'José Caballero', 'Jose Caballero', 'J.C. Escarra', 'JC Escarra'):
            with self.subTest(text=text):
                self.assertIsNotNone(self.reason(text))

    def test_first_names_and_ambiguous_names_do_not_block(self):
        for text in ('Aaron made a great throw', 'Max is back', 'Will play tonight',
                     'Ben is ready', 'Judge made the call', 'Rice is back',
                     'Cole had a good day', 'Hill looks healthy', 'OG is back',
                     'Deuce looks good', 'All rise', 'Gerrit is ready'):
            with self.subTest(text=text):
                self.assertIsNone(self.reason(text))

    def test_mlb_context_requires_specific_player_evidence(self):
        for text in ('Judge hit a homer', 'Cole left after six innings',
                     'Goldy had three RBI', 'Lindor had two at bats'):
            with self.subTest(text=text):
                self.assertIsNotNone(self.reason(text))
        for text in ('Aaron is playing baseball', 'Great throw by Aaron',
                     'Max went to a game', 'A good baseball game tonight'):
            with self.subTest(text=text):
                self.assertIsNone(self.reason(text))

    def test_football_context_wins_even_with_strong_baseball_evidence(self):
        for text in ('Aaron Judge met a quarterback', 'Yankees players love football',
                     'Judge hit a homer while Nabers scored a touchdown',
                     'Knicks welcomed the WR', 'NFL fans watched KAT dunk'):
            with self.subTest(text=text):
                self.assertIsNone(self.reason(text))

    def test_ordinary_words_are_not_players_even_in_sports_prose(self):
        for text in ("John Harbaugh's hire will be judged years later",
                     "I'll be the judge of how the Giants did this week",
                     "I'll be the judge of this baseball game",
                     'The MLB decision will be judged years later',
                     'Baseball fans cannot judge that decision yet',
                     'Rice and beans before watching baseball',
                     'Basketball builds bridges between communities',
                     'The glass at the hockey rink broke',
                     'All rise before the baseball game',
                     'The Martian is a movie for baseball fans',
                     'An OG basketball fan', 'A hockey fan saw a fox'):
            with self.subTest(text=text):
                self.assertIsNone(self.reason(text))

    def test_ambiguous_names_require_direct_athlete_action(self):
        for text in ('Judge just homered', 'The Judge hit another home run',
                     'Rice hit a homer', 'Cole pitched six innings',
                     'Deuce dunked in the NBA game', 'Glass scored in the NHL game'):
            with self.subTest(text=text):
                self.assertIsNotNone(self.reason(text))

    def test_commentary_and_quote_do_not_invent_player_references(self):
        self.assertIsNone(self.reason('I will judge', quote='Homered in baseball yesterday'))
        self.assertIsNone(self.reason('MLB fans will judge the decision', quote='I disagree'))
        self.assertIsNotNone(self.reason('MLB fans will judge the decision', quote='Judge homered'))
        self.assertIsNotNone(self.reason('MLB fans will judge the decision but Lindor had two at bats'))

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
