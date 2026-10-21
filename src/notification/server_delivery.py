"""Persist server suspension without changing subscriptions or notifier settings."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

PREFIX = 'server_delivery:'


class ServerDelivery:
    def __init__(self):
        self.states = {}
        self.active = {}
        self.idle = {}

    async def load(self, db_path):
        async with aiosqlite.connect(db_path, timeout=10) as db:
            rows = await (await db.execute(
                'SELECT key,value FROM runtime_setting WHERE key LIKE ?', (PREFIX+'%',)
            )).fetchall()
        for key, value in rows:
            self.states[key[len(PREFIX):]] = json.loads(value)

    def enabled(self, server_id):
        return self.states.get(str(server_id), {}).get('enabled', True)

    def allows(self, server_id, tweet):
        state = self.states.get(str(server_id), {})
        if not state.get('enabled', True):
            return False
        cutoff = state.get('resume_after')
        if not cutoff:
            return True
        created = tweet.created_on
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace('Z', '+00:00'))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created > datetime.fromisoformat(cutoff)

    async def set_enabled(self, db_path, server_id, enabled, now=None):
        server_id = str(server_id)
        if self.enabled(server_id) == enabled:
            return False
        state = dict(self.states.get(server_id, {}), enabled=enabled)
        if enabled:
            state['resume_after'] = (now or datetime.now(timezone.utc)).isoformat()
        # Publish the pause immediately, including while SQLite is busy. Roll
        # back on failure; an enable is published only after it is durable.
        previous = self.states.get(server_id)
        if not enabled:
            self.states[server_id] = state
        try:
            async with aiosqlite.connect(db_path, timeout=10) as db:
                await db.execute('INSERT OR REPLACE INTO runtime_setting VALUES (?,?)',
                                 (PREFIX+server_id, json.dumps(state)))
                await db.commit()
        except BaseException:
            if previous is None:
                self.states.pop(server_id, None)
            else:
                self.states[server_id] = previous
            raise
        self.states[server_id] = state
        return True

    @asynccontextmanager
    async def delivery(self, server_id, tweet):
        """Allow concurrent channels, but let disable drain whole in-flight tweets."""
        server_id = str(server_id)
        allowed = self.allows(server_id, tweet)
        if allowed:
            self.active[server_id] = self.active.get(server_id, 0) + 1
            event = self.idle.setdefault(server_id, asyncio.Event())
            event.clear()
        try:
            yield allowed
        finally:
            if allowed:
                self.active[server_id] -= 1
                if not self.active[server_id]:
                    self.idle[server_id].set()

    async def drain(self, server_id):
        server_id = str(server_id)
        if self.active.get(server_id, 0):
            await self.idle[server_id].wait()
