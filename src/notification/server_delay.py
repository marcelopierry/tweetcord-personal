"""Persistent server-specific send delays with a one-time global migration."""
import aiosqlite

PREFIX = 'notification_delay_seconds:'
MIGRATED = 'server_send_delay_migrated'


class ServerDelays:
    def __init__(self, default_seconds=180):
        self.default_seconds = max(0, int(default_seconds))
        self.values = {}

    def get(self, server_id):
        return self.values.get(str(server_id), self.default_seconds)

    async def load(self, db_path, server_ids=()):
        async with aiosqlite.connect(db_path, timeout=10) as db:
            await db.execute('BEGIN IMMEDIATE')
            marker = await (await db.execute('SELECT value FROM runtime_setting WHERE key=?', (MIGRATED,))).fetchone()
            if not marker:
                old = await (await db.execute("SELECT value FROM runtime_setting WHERE key='notification_delay_seconds'")).fetchone()
                try:
                    inherited = max(0, int(old[0])) if old else self.default_seconds
                except (ValueError, TypeError):
                    inherited = self.default_seconds
                known = {str(value) for value in server_ids}
                known.update(row[0] for row in await (await db.execute('SELECT DISTINCT server_id FROM channel')).fetchall())
                for server_id in known:
                    await db.execute('INSERT OR IGNORE INTO runtime_setting VALUES (?,?)',
                                     (PREFIX+str(server_id), str(inherited)))
                await db.execute('INSERT OR REPLACE INTO runtime_setting VALUES (?,?)', (MIGRATED, '1'))
            rows = await (await db.execute('SELECT key,value FROM runtime_setting WHERE key LIKE ?', (PREFIX+'%',))).fetchall()
            await db.commit()
        self.values = {}
        for key, value in rows:
            try:
                self.values[key[len(PREFIX):]] = max(0, int(value))
            except (ValueError, TypeError):
                pass

    async def set(self, db_path, server_id, seconds):
        seconds = max(0, int(seconds))
        async with aiosqlite.connect(db_path, timeout=10) as db:
            await db.execute('INSERT OR REPLACE INTO runtime_setting VALUES (?,?)',
                             (PREFIX+str(server_id), str(seconds)))
            await db.commit()
        self.values[str(server_id)] = seconds
