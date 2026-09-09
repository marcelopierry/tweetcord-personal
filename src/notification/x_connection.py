"""Single-loop, bounded X requests with recovery from broken transports."""
import asyncio
import httpx


class XConnection:
    def __init__(self, client, factory, timeout=20):
        self._client = client
        self._me = client.me
        self._factory = factory
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self.request = self

    @property
    def me(self):
        return self._me

    async def _discard(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.request.session.aclose(), 2)
            except Exception:
                pass

    async def _call(self, method, *args, request=False):
        async with self._lock:
            try:
                if self._client is None:
                    self._client = await asyncio.wait_for(self._factory(), self._timeout)
                    self._me = self._client.me
                target = self._client.request if request else self._client
                return await asyncio.wait_for(getattr(target, method)(*args), self._timeout)
            except (TimeoutError, httpx.TransportError, asyncio.CancelledError):
                # Cancellation can leave HTTP/2 stream state unusable. Never reuse it.
                await self._discard()
                raise

    async def get_tweet_notifications(self):
        return await self._call('get_tweet_notifications')

    async def get_tweet_detail(self, tweet_id):
        return await self._call('get_tweet_detail', tweet_id, request=True)

    async def get_friendship(self, source_id, target_id):
        return await self._call('get_friendship', source_id, target_id, request=True)

    async def follow_user(self, user_id):
        return await self._call('follow_user', user_id)

    async def enable_user_notification(self, user_id):
        return await self._call('enable_user_notification', user_id)
