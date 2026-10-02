"""Channel-local content rules, evaluated against the final refreshed text."""
import html
import json
import re
import unicodedata
from pathlib import Path


def normalize(text):
    text = html.unescape(str(text or '')).casefold()
    text = ''.join(char for char in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(char))
    # Strip Markdown URL targets, preserving the visible label. URL slugs alone
    # must not trigger a block or accidentally bypass one.
    text = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' ', text)
    text = text.replace('\\', '').replace('.', '').replace('’', "'")
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\']+', ' ', text)).strip()


def compile_terms(terms):
    normalized = sorted({normalize(term) for term in terms} - {''}, key=len, reverse=True)
    return re.compile(r'(?<![a-z0-9])(?:' + '|'.join(map(re.escape, normalized)) + r')(?![a-z0-9])')


def player_terms(players):
    for name in players:
        yield name
        # A full player name remains specific even without Jr./II/III.
        yield re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name)


SPORT_CONTEXT = {
    'baseball': ['baseball', 'MLB', 'home run', 'home runs', 'homer', 'homers',
                 'homered', 'bullpen', 'inning', 'innings', 'pitcher', 'pitchers',
                 'strikeout', 'strikeouts', 'batting', 'at bat', 'at bats', 'RBI'],
    'basketball': ['basketball', 'NBA', 'three pointer', 'three pointers',
                   'three point', 'dunk', 'dunks', 'dunked', 'free throw', 'free throws'],
    'hockey': ['hockey', 'NHL', 'puck', 'goalie', 'goalies', 'power play', 'slapshot'],
}

# Words/phrases that can be ordinary prose or generic nicknames. Mere sport
# co-occurrence is insufficient; require a directly attached athlete action.
AMBIGUOUS_NAMES = {
    'judge', 'the judge', 'rice', 'hill', 'wells', 'cole', 'fried', 'morel',
    'young', 'stock', 'brown', 'glass', 'fox', 'wolf', 'bridges', 'towns',
    'miller', 'warren', 'jones', 'allen', 'scott', 'hart', 'mann', 'mayfield',
    'johnson', 'nelson', 'ellis', 'porter', 'powell', 'sharpe', 'burns',
    'day day', 'big g', 'og', 'jb', 'kat', 'deuce', 'dougie', 'all rise',
    'allrise', 'white whale', 'the martian',
}
ATHLETE_ACTION = {
    'baseball': re.compile(
        r'(?:homered|homers|struck out|pitched|batting|at bat|'
        r'left after (?:\w+ ){1,3}innings|'
        r'(?:hit|hits|hitting) (?:\w+ ){0,3}(?:homer|homers|home run|home runs)|'
        r'(?:drove|drives) in (?:\w+ ){0,2}(?:run|runs)|'
        r'(?:had|has) (?:\w+ ){0,2}(?:rbi|at bats))\b'),
    'basketball': re.compile(
        r'(?:dunked|dunks|dunking|hit (?:a |another )?three pointer|'
        r'(?:made|makes) (?:\w+ ){0,2}(?:free throw|free throws))\b'),
    'hockey': re.compile(
        r'(?:scored|scores|scoring|saved|saves|stopped|stops|'
        r'fired (?:a |another )?slapshot)\b'),
}


def clearly_refers_to_player(text, match, sport):
    if match.group(0) not in AMBIGUOUS_NAMES:
        return True
    tail = text[match.end():].lstrip()
    if ATHLETE_ACTION[sport].match(tail):
        return True
    # Recognize "Judge just homered" but not "I'll judge the baseball game".
    tail = re.sub(r'^(?:(?:just|has|had|will|is|was) ){0,2}', '', tail)
    return bool(ATHLETE_ACTION[sport].match(tail))


class ChannelContentFilter:
    def __init__(self, rules):
        self.channel_id = str(rules['channel_id'])
        self.exempt_accounts = {name.casefold().lstrip('@') for name in rules['exempt_accounts']}
        self.allowed = compile_terms(rules['allow_terms'])
        names = [name for roster in rules['blocked_players'].values() for name in roster]
        self.blocked = compile_terms([
            *rules['blocked_teams'], *player_terms(names),
        ])
        # Bare first names never block. Surnames and nicknames are only evidence
        # when the same text also clearly discusses that player's sport.
        self.contextual = []
        for sport, teams in (
            ('baseball', ('Yankees', 'Mets')),
            ('basketball', ('Knicks', 'Nets')),
            ('hockey', ('Rangers', 'Islanders', 'Devils')),
        ):
            surnames = [re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name).split()[-1]
                        for team in teams for name in rules['blocked_players'][team]]
            aliases = rules['contextual_aliases'][sport]
            self.contextual.append((sport, compile_terms(SPORT_CONTEXT[sport]),
                                    compile_terms([*surnames, *aliases])))

    def blocked_reason(self, channel_id, username, tweet, parsed_tweet=None):
        if str(channel_id) != self.channel_id or username.casefold().lstrip('@') in self.exempt_accounts:
            return None
        # Parsed text is refreshed immediately before delivery; don't also mix
        # in the old feed text, which may have changed since discovery.
        if parsed_tweet is not None:
            pieces = [getattr(parsed_tweet, 'text', None),
                      getattr(getattr(parsed_tweet, 'quote', None), 'text', None)]
        else:
            pieces = []
            seen = set()
            def collect(post):
                if post is None or id(post) in seen:
                    return
                seen.add(id(post))
                pieces.append(getattr(post, 'text', None))
                collect(getattr(post, 'retweeted_tweet', None))
                collect(getattr(post, 'quoted_tweet', None))
            collect(tweet)
        texts = [normalize(piece) for piece in pieces if piece]
        if any(self.allowed.search(text) for text in texts):
            return None
        # Do not combine unrelated words from the commentary and quoted post
        # into invented names or player/action phrases.
        for text in texts:
            match = self.blocked.search(text)
            if match:
                return match.group(0)
            for sport, context, names in self.contextual:
                if context.search(text):
                    for match in names.finditer(text):
                        if clearly_refers_to_player(text, match, sport):
                            return f'{match.group(0)} ({sport} context)'
        return None


GIANTS_CHANNEL_FILTER = ChannelContentFilter(json.loads(
    Path(__file__).with_name('giants_filter_rules.json').read_text(encoding='utf-8')
))
