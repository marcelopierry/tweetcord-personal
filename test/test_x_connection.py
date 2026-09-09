import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from src.notification.x_connection import XConnection


def client():
    return SimpleNamespace(me=SimpleNamespace(id='1'),
        request=SimpleNamespace(session=SimpleNamespace(aclose=AsyncMock()),
                                get_tweet_detail=AsyncMock(return_value={'ok': True})),
        get_tweet_notifications=AsyncMock(return_value=['post']))


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_stay_on_same_loop(self):
        original = client()
        loop = asyncio.get_running_loop()
        async def poll():
            self.assertIs(asyncio.get_running_loop(), loop)
            return ['post']
        original.get_tweet_notifications.side_effect = poll
        connection = XConnection(original, AsyncMock())
        self.assertEqual(await connection.get_tweet_notifications(), ['post'])

    async def test_protocol_error_replaces_client_next_request(self):
        original, fresh = client(), client()
        original.get_tweet_notifications.side_effect = httpx.LocalProtocolError('broken')
        factory = AsyncMock(return_value=fresh)
        connection = XConnection(original, factory)
        with self.assertRaises(httpx.LocalProtocolError):
            await connection.get_tweet_notifications()
        original.request.session.aclose.assert_awaited_once()
        self.assertEqual(await connection.get_tweet_notifications(), ['post'])
        factory.assert_awaited_once()

    async def test_hung_request_times_out_and_recovers(self):
        original, fresh = client(), client()
        async def hang():
            await asyncio.Event().wait()
        original.get_tweet_notifications.side_effect = hang
        connection = XConnection(original, AsyncMock(return_value=fresh), timeout=0.01)
        with self.assertRaises(TimeoutError):
            await connection.get_tweet_notifications()
        self.assertEqual(await connection.get_tweet_notifications(), ['post'])

    async def test_external_validation_cancellation_discards_transport(self):
        original, fresh = client(), client()
        started = asyncio.Event()
        async def hang(tweet_id):
            started.set()
            await asyncio.Event().wait()
        original.request.get_tweet_detail.side_effect = hang
        connection = XConnection(original, AsyncMock(return_value=fresh))
        task = asyncio.create_task(connection.get_tweet_detail('42'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await connection.get_tweet_detail('42'), {'ok': True})

    async def test_application_error_does_not_reset_authentication(self):
        original = client()
        original.get_tweet_notifications.side_effect = ValueError('bad data')
        factory = AsyncMock()
        connection = XConnection(original, factory)
        with self.assertRaises(ValueError):
            await connection.get_tweet_notifications()
        factory.assert_not_called()
        original.request.session.aclose.assert_not_called()

    async def test_requests_are_serialized(self):
        original = client()
        active = 0
        async def poll():
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            await asyncio.sleep(0.001)
            active -= 1
            return []
        original.get_tweet_notifications.side_effect = poll
        connection = XConnection(original, AsyncMock())
        await asyncio.gather(*(connection.get_tweet_notifications() for _ in range(3)))


if __name__ == '__main__':
    unittest.main()
