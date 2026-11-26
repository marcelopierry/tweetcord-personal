"""Server-managed, case-insensitive substring filters for tweet delivery."""

import asyncio
import html
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

import aiosqlite


def normalize(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', html.unescape(text)).casefold().split())


def visible_text(text: str) -> str:
    # ParsedTweet generates links for mentions and hashtags, and ** for bold.
    # Preserve literal punctuation in their labels and in expanded URLs.
    text = re.sub(r'(?<!\\)\[([^\]\n]+)\]\(https?://[^\s)]+\)', r'\1', text)
    text = re.sub(r'(?<!\\)\*\*', '', text)
    return re.sub(r'\\([\\*_~|`\[\]()#>+-])', r'\1', text)


def tweet_texts(tweet, parsed):
    original = getattr(tweet, 'retweeted_tweet', None) or tweet
    quoted = getattr(original, 'quoted_tweet', None) or getattr(tweet, 'quoted_tweet', None)
    for source, rendered in ((original, parsed), (quoted, getattr(parsed, 'quote', None))):
        body = getattr(rendered, 'text', None)
        if body is None:
            body = getattr(source, 'text', None)
        else:
            body = visible_text(body)
        if body:
            yield normalize(body)
        translated = getattr(rendered, 'trans_text', None)
        if translated:
            yield normalize(visible_text(translated))


@dataclass(frozen=True)
class SpoilerRule:
    id: int
    server_id: str
    term: str
    term_key: str
    account: str
    channel_id: str
    expires_at: float | None

    def active(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


class SpoilerBlocks:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._rules: dict[str, dict[int, SpoilerRule]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as db:
                rows = await db.execute_fetchall(
                    'SELECT id, server_id, term, term_key, account, channel_id, expires_at '
                    'FROM spoiler_block WHERE expires_at IS NULL OR expires_at > ?', (time.time(),),
                )
            self._rules.clear()
            for row in rows:
                rule = SpoilerRule(*row)
                self._rules.setdefault(rule.server_id, {})[rule.id] = rule

    async def add(
        self, server_id: int, term: str, *, account: str | None = None,
        channel_id: int | None = None, days: int | None = None,
    ) -> SpoilerRule:
        term = ' '.join(term.split())
        term_key = normalize(term)
        if not term_key or len(term) > 120:
            raise ValueError('Enter a word or phrase between 1 and 120 characters.')
        account_key = (account or '').strip().removeprefix('@').casefold()
        if account is not None and not re.fullmatch(r'[a-z0-9_]{1,15}', account_key):
            raise ValueError('Enter an X handle such as @DiscussingFilm.')
        if days is not None and days < 1:
            raise ValueError('Enter at least one day, or leave days blank for no expiration.')
        channel = str(channel_id) if channel_id is not None else ''
        async with self._lock:
            expires_at = time.time() + timedelta(days=days).total_seconds() if days is not None else None
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                async with db.execute(
                    'INSERT INTO spoiler_block (server_id, term, term_key, account, channel_id, expires_at) '
                    'VALUES (?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT (server_id, term_key, account, channel_id) DO UPDATE SET '
                    'term=excluded.term, expires_at=excluded.expires_at '
                    'RETURNING id, server_id, term, term_key, account, channel_id, expires_at',
                    (str(server_id), term, term_key, account_key, channel, expires_at),
                ) as cursor:
                    rule = SpoilerRule(*await cursor.fetchone())
                await db.commit()
            self._rules.setdefault(rule.server_id, {})[rule.id] = rule
        return rule

    async def remove(self, server_id: int, rule_id: int) -> bool:
        async with self._lock:
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                async with db.execute(
                    'DELETE FROM spoiler_block WHERE server_id=? AND id=?', (str(server_id), rule_id),
                ) as cursor:
                    removed = cursor.rowcount > 0
                await db.commit()
            self._rules.get(str(server_id), {}).pop(rule_id, None)
        return removed

    def active_rules(self, server_id: int | str) -> list[SpoilerRule]:
        now = time.time()
        return [rule for rule in self._rules.get(str(server_id), {}).values() if rule.active(now)]

    def blocked_rule(
        self, server_id: int | str, channel_id: int, account: str, tweet, parsed=None,
    ) -> SpoilerRule | None:
        account = account.removeprefix('@').casefold()
        channel = str(channel_id)
        applicable = [
            rule for rule in self.active_rules(server_id)
            if (not rule.channel_id or rule.channel_id == channel)
            and (not rule.account or rule.account == account)
        ]
        if not applicable:
            return None
        texts = tuple(tweet_texts(tweet, parsed))
        return next((rule for rule in applicable if any(rule.term_key in body for body in texts)), None)
