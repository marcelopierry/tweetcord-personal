import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.notification.notifier_commands import notifier_settings_unchanged, selected_interaction_channel_id


class TestNotifierCommandHelpers(unittest.TestCase):
    def test_same_enabled_settings_are_reported_as_unchanged(self):
        existing = {
            'enabled': 1,
            'role_id': '',
            'enable_type': '11',
            'enable_media_type': '11',
        }
        self.assertTrue(notifier_settings_unchanged(existing, '', '11', '11'))

    def test_changed_or_disabled_settings_are_not_reported_as_unchanged(self):
        existing = {
            'enabled': 1,
            'role_id': '123',
            'enable_type': '11',
            'enable_media_type': '11',
        }
        self.assertFalse(notifier_settings_unchanged(existing, '', '11', '11'))
        existing['enabled'] = 0
        self.assertFalse(notifier_settings_unchanged(existing, '123', '11', '11'))

    def test_remove_channel_defaults_to_interaction_channel(self):
        interaction = SimpleNamespace(channel_id=321, data={'options': []})
        self.assertEqual(selected_interaction_channel_id(interaction), '321')

    def test_explicit_remove_channel_wins_over_current_channel(self):
        interaction = SimpleNamespace(channel_id=321, data={'options': []})
        self.assertEqual(selected_interaction_channel_id(interaction, '654'), '654')

    def test_autocomplete_finds_named_channel_option_in_any_position(self):
        interaction = SimpleNamespace(
            channel_id=321,
            data={'options': [{'options': [
                {'name': 'username', 'value': 'example'},
                {'name': 'channel', 'value': '987'},
            ]}]},
        )
        self.assertEqual(selected_interaction_channel_id(interaction), '987')


if __name__ == '__main__':
    unittest.main()
