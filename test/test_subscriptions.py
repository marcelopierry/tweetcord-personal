import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from src.notification.subscriptions import ensure_subscription, subscription_state


def response(following=True, notifications=True):
    return {'relationship': {'source': {'following': following, 'notifications_enabled': notifications},
                             'target': {'id_str': '1605'}}}


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *states):
        return SimpleNamespace(me=SimpleNamespace(id='123'),
            request=SimpleNamespace(get_friendship=AsyncMock(side_effect=states)),
            follow_user=AsyncMock(), enable_user_notification=AsyncMock())

    async def test_healthy_does_not_write(self):
        client = self.client(response())
        self.assertFalse(await ensure_subscription(client, '1605'))
        client.follow_user.assert_not_called()
        client.enable_user_notification.assert_not_called()

    async def test_sama_notifications_off_is_repaired_and_verified(self):
        client = self.client(response(notifications=False), response())
        self.assertTrue(await ensure_subscription(client, '1605'))
        client.follow_user.assert_not_called()
        client.enable_user_notification.assert_awaited_once_with('1605')
        self.assertEqual(client.request.get_friendship.await_count, 2)

    async def test_missing_follow_repaired(self):
        client = self.client(response(False, False), response())
        self.assertTrue(await ensure_subscription(client, '1605'))
        client.follow_user.assert_awaited_once_with('1605')

    async def test_silent_write_failure_is_not_success(self):
        client = self.client(response(notifications=False), response(notifications=False))
        with self.assertRaises(RuntimeError):
            await ensure_subscription(client, '1605')

    async def test_unknown_state_never_triggers_write(self):
        client = self.client({})
        with self.assertRaises(RuntimeError):
            await ensure_subscription(client, '1605')
        client.enable_user_notification.assert_not_called()

    async def test_wrong_target_rejected(self):
        client = self.client(response())
        with self.assertRaises(RuntimeError):
            await subscription_state(client, '999')

    async def test_network_failure_is_not_healthy(self):
        client = self.client(TimeoutError())
        with self.assertRaises(TimeoutError):
            await ensure_subscription(client, '1605')


if __name__ == '__main__':
    unittest.main()
