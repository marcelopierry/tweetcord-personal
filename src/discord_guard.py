"""Persist a conservative limit on fresh Discord IDENTIFY sessions."""
import asyncio
import os
import time
import aiosqlite
from discord.ext import commands
from src.log import setup_logger

log = setup_logger(__name__)


class SessionStartGuard:
    def __init__(self, path, limit=20, window=86400, spacing=60):
        self.path, self.limit, self.window, self.spacing = path, limit, window, spacing

    async def reserve(self, now=None):
        now = time.time() if now is None else now
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute('CREATE TABLE IF NOT EXISTS session_start (started REAL NOT NULL)')
            await db.execute('BEGIN IMMEDIATE')
            await db.execute('DELETE FROM session_start WHERE started <= ?', (now-self.window,))
            starts = [row[0] for row in await (await db.execute(
                'SELECT started FROM session_start ORDER BY started')).fetchall()]
            wait = max(0, starts[-1]+self.spacing-now) if starts else 0
            if len(starts) >= self.limit:
                wait = max(wait, starts[0]+self.window-now)
            if not wait:
                await db.execute('INSERT INTO session_start VALUES (?)', (now,))
            await db.commit()
            return wait

    async def wait(self):
        warned = False
        while True:
            delay = await self.reserve()
            if not delay:
                return
            if not warned:
                log.warning(f'Discord session-start safety limit: waiting {delay:.0f}s; '
                            'not opening another session')
                warned = True
            await asyncio.sleep(min(delay, 60))


class ProtectedBot(commands.Bot):
    async def setup_hook(self):
        # Executed once per process, unlike on_ready which repeats on reconnect.
        if getattr(self, '_startup_complete', False):
            return
        await self.initialize_once()
        self._startup_complete = True

    async def before_identify_hook(self, shard_id, *, initial=False):
        os.makedirs(os.environ['DATA_PATH'], exist_ok=True)
        guard = SessionStartGuard(os.path.join(os.environ['DATA_PATH'], 'discord-session-starts.db'))
        await guard.wait()
        await super().before_identify_hook(shard_id, initial=initial)
