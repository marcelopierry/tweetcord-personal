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
        parts = re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name).split()
        if len(parts) >= 2:
            yield parts[0]  # Deliberately include common first names, as requested.
            yield parts[0].split('-')[0]
            yield ' '.join(parts[1:])
            yield parts[-1]


class ChannelContentFilter:
    def __init__(self, rules):
        self.channel_id = str(rules['channel_id'])
        self.exempt_accounts = {name.casefold().lstrip('@') for name in rules['exempt_accounts']}
        self.allowed = compile_terms(rules['allow_terms'])
        names = [name for roster in rules['blocked_players'].values() for name in roster]
        self.blocked = compile_terms([
            *rules['blocked_teams'], *rules['blocked_aliases'], *player_terms(names),
        ])

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
        text = normalize('\n'.join(str(piece) for piece in pieces if piece))
        if self.allowed.search(text):
            return None
        match = self.blocked.search(text)
        return match.group(0) if match else None


GIANTS_CHANNEL_FILTER = ChannelContentFilter(json.loads(
    Path(__file__).with_name('giants_filter_rules.json').read_text(encoding='utf-8')
))
