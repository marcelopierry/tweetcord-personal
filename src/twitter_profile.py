from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
from tweety.exceptions import UserNotFound

from src.log import setup_logger


log = setup_logger(__name__)
PROFILE_URL = 'https://api.fxtwitter.com/2/profile/{handle}'


@dataclass(frozen=True)
class ResolvedUser:
    id: str
    username: str
    name: str
    profile_image_url_https: str | None = None
    protected: bool = False


async def resolve_user(app, username: str) -> ResolvedUser:
    """Resolve a public X profile without consuming the authenticated lookup limit.

    FxTwitter is already used by the bot for post/media parsing. Its profile API
    provides the numeric ID needed by Tweety's follow and notification methods.
    If FxTwitter itself is temporarily unavailable, fall back to Tweety.
    """
    handle = username.strip().lstrip('@')
    if not handle:
        raise UserNotFound()

    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                PROFILE_URL.format(handle=quote(handle, safe='')),
                headers={'User-Agent': 'Personal-TweetCord/1.0'},
            ) as response:
                if response.status == 404:
                    raise UserNotFound()
                if response.status == 200:
                    payload = await response.json(content_type=None)
                    profile = payload.get('user') or {}
                    user_id = profile.get('id')
                    screen_name = profile.get('screen_name')
                    if user_id and screen_name:
                        return ResolvedUser(
                            id=str(user_id),
                            username=str(screen_name),
                            name=str(profile.get('name') or screen_name),
                            profile_image_url_https=profile.get('avatar_url'),
                            protected=bool(profile.get('protected', False)),
                        )
    except UserNotFound:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        log.warning(f'FxTwitter profile lookup failed for {handle}; using authenticated X lookup: {error}')

    return await app.get_user_info(handle)
