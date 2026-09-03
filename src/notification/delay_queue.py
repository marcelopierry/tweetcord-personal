from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _tweet_key(tweet: Any) -> str:
    """Return a stable key for a Tweet, even when a test double lacks ``id``."""
    return str(getattr(tweet, "id", None) or getattr(tweet, "url", ""))


@dataclass
class PendingTweet:
    tweet: Any
    due_at: datetime


class DelayedTweetBuffer:
    """Hold tweets long enough for short-lived edits/deletions to settle.

    The scraper returns a fresh Tweet object on each poll. Replacing the object
    while it is pending means an edit observed during the delay is what gets
    delivered. The buffer is deliberately in-memory; the account timestamp is
    still written to SQLite only after a pending batch is released.
    """

    def __init__(self, delay_seconds: int = 300):
        self.delay = timedelta(seconds=max(0, int(delay_seconds)))
        self._items: dict[str, PendingTweet] = {}

    def add(self, tweet: Any, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        key = _tweet_key(tweet)
        if not key:
            return

        existing = self._items.get(key)
        if existing is None:
            self._items[key] = PendingTweet(tweet=tweet, due_at=now + self.delay)
        else:
            # Keep the original due time but use the newest representation.
            existing.tweet = tweet

    def pop_ready(self, now: datetime | None = None) -> list[Any]:
        now = now or datetime.now(timezone.utc)
        ready_keys = [key for key, item in self._items.items() if item.due_at <= now]
        ready = [self._items.pop(key).tweet for key in ready_keys]
        return sorted(ready, key=lambda tweet: getattr(tweet, "created_on", now))

    def defer(self, tweet: Any, retry_seconds: int = 60, now: datetime | None = None) -> None:
        """Requeue a ready tweet for a short validation retry."""
        now = now or datetime.now(timezone.utc)
        key = _tweet_key(tweet)
        if not key:
            return
        self._items[key] = PendingTweet(
            tweet=tweet,
            due_at=now + timedelta(seconds=max(1, int(retry_seconds))),
        )

    def set_delay_seconds(self, delay_seconds: int) -> None:
        """Apply a new delay to existing and future items retroactively."""
        new_delay = timedelta(seconds=max(0, int(delay_seconds)))
        delta = new_delay - self.delay
        self.delay = new_delay
        for item in self._items.values():
            item.due_at += delta

    def __len__(self) -> int:
        return len(self._items)
