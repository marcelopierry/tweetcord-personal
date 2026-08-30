import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.db_function.init_db import init_db


class TestInitDbMigrations(unittest.IsolatedAsyncioTestCase):
    async def test_existing_database_receives_new_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, 'tracked_accounts.db')
            async with aiosqlite.connect(db_path) as db:
                await db.execute('CREATE TABLE user (id TEXT PRIMARY KEY)')
                await db.commit()

            with patch.dict(os.environ, {'DATA_PATH': directory}):
                await init_db()

            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ) as cursor:
                    tables = {row[0] async for row in cursor}

            self.assertIn('runtime_setting', tables)
            self.assertIn('delivered_tweet', tables)


if __name__ == '__main__':
    unittest.main()
