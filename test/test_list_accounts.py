git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-YQAxNmwC' (errno=Operation not permitted)
2026-09-02 22:51:13.637 xcodebuild[56286:5959616]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:13.764 xcodebuild[56286:5959615] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-bfGOI5gH' (errno=Operation not permitted)
2026-09-02 22:51:14.144 xcodebuild[56290:5959627]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:14.268 xcodebuild[56290:5959626] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from cogs.list_users import ListUsers


class TestListAccountsChannelFilter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_data_path = os.environ.get('DATA_PATH')
        os.environ['DATA_PATH'] = self.temp_dir.name

        db_path = os.path.join(self.temp_dir.name, 'tracked_accounts.db')
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE user (id TEXT PRIMARY KEY, username TEXT);
                CREATE TABLE channel (id TEXT PRIMARY KEY, server_id TEXT);
                CREATE TABLE notification (user_id TEXT, channel_id TEXT, enabled INTEGER);

                INSERT INTO user VALUES ('1', 'Alpha'), ('2', 'Beta'), ('3', 'Disabled'), ('4', 'OtherServer');
                INSERT INTO channel VALUES ('10', '123'), ('20', '123'), ('30', '456');
                INSERT INTO notification VALUES
                    ('1', '10', 1),
                    ('1', '20', 1),
                    ('2', '20', 1),
                    ('3', '10', 0),
                    ('4', '30', 1);
            """)

    def tearDown(self):
        if self.previous_data_path is None:
            os.environ.pop('DATA_PATH', None)
        else:
            os.environ['DATA_PATH'] = self.previous_data_path
        self.temp_dir.cleanup()

    def test_defaults_to_all_server_channels_and_deduplicates_accounts(self):
        usernames = asyncio.run(ListUsers._get_account_usernames(None, 123))
        self.assertEqual(usernames, ['Alpha', 'Beta'])

    def test_channel_filter_only_returns_accounts_for_selected_channel(self):
        usernames = asyncio.run(ListUsers._get_account_usernames(None, 123, '10'))
        self.assertEqual(usernames, ['Alpha'])

    def test_channel_filter_cannot_escape_the_selected_server(self):
        usernames = asyncio.run(ListUsers._get_account_usernames(None, 123, '30'))
        self.assertEqual(usernames, [])

    def test_duplicate_legacy_users_command_is_removed(self):
        command_names = {command.name for command in ListUsers.list_group.commands}
        self.assertEqual(command_names, {'accounts', 'settings'})


if __name__ == '__main__':
    unittest.main()
