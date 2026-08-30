from __future__ import annotations

import aiosqlite

from src.utils import get_lock, get_utcnow


lock = get_lock()


class DeliveryHistory:
    """Persistent, channel-specific record of tweets already shown in Discord."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def claim(self, channel_id: str | int, tweet_id: str | int) -> bool:
        """Atomically reserve a tweet ID; false means that channel saw it already."""
        async with lock:
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                cursor = await db.execute(
                    'INSERT OR IGNORE INTO delivered_tweet (channel_id, tweet_id, delivered_at) VALUES (?, ?, ?)',
                    (str(channel_id), str(tweet_id), get_utcnow()),
                )
                await db.commit()
                return cursor.rowcount == 1

    async def record(self, channel_id: str | int, *tweet_ids: str | int | None) -> None:
        rows = [
            (str(channel_id), str(tweet_id), get_utcnow())
            for tweet_id in dict.fromkeys(tweet_ids)
            if tweet_id is not None
        ]
        if not rows:
            return
        async with lock:
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                await db.executemany(
                    'INSERT OR IGNORE INTO delivered_tweet (channel_id, tweet_id, delivered_at) VALUES (?, ?, ?)',
                    rows,
                )
                await db.commit()

    async def release(self, channel_id: str | int, tweet_id: str | int) -> None:
        """Release a failed reservation so a later poll can retry delivery."""
        async with lock:
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                await db.execute(
                    'DELETE FROM delivered_tweet WHERE channel_id = ? AND tweet_id = ?',
                    (str(channel_id), str(tweet_id)),
                )
                await db.commit()
