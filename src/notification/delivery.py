from __future__ import annotations

import asyncio
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord

from core.classes import ParsedTweet
from src.log import setup_logger


log = setup_logger(__name__)

WEBHOOK_NAME = 'Personal TweetCord delivery'
WEBHOOK_SUFFIX = 'Personal TweetCord'
MAX_ATTACHMENTS = 10


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    filename: str
    size: int | None = None
    bitrate: int | None = None


@dataclass
class MediaDelivery:
    files: list[discord.File]
    fallback_urls: list[str]


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build_delivery_links(tweet_url: str, parsed_tweet: ParsedTweet | None, is_quote: bool) -> str:
    """Return embed-suppressed post links, putting a quoted original first."""
    urls = []
    quote_url = parsed_tweet.quote.url if parsed_tweet and parsed_tweet.quote else None
    if is_quote and quote_url:
        urls.append(quote_url)
    if tweet_url:
        urls.append(tweet_url)

    # Preserve order while avoiding duplicate links when the source is malformed.
    return '\n'.join(f'<{url}>' for url in dict.fromkeys(urls))


def _large_avatar(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r'_(?:normal|bigger|mini|\d+x\d+)(?=\.(?:jpg|jpeg|png|webp)(?:\?|$))', '_400x400', url, flags=re.IGNORECASE)


def build_webhook_identity(tracked_username: str, tweet: Any, parsed_tweet: ParsedTweet | None) -> tuple[str, str | None]:
    """Use the tracked account's identity, including when it is a retweet."""
    tracked = tracked_username.casefold()
    parsed_name = getattr(parsed_tweet, 'sender_name', None)
    parsed_username = getattr(parsed_tweet, 'sender_username', None)
    parsed_avatar = getattr(parsed_tweet, 'sender_avatar_url', None)

    if parsed_name and (not parsed_username or parsed_username.casefold() == tracked):
        name, avatar = parsed_name, parsed_avatar
    else:
        author = getattr(tweet, 'author', None)
        author_name = getattr(author, 'name', None)
        author_username = getattr(author, 'username', None)
        author_avatar = getattr(author, 'profile_image_url_https', None)
        if author_name and (not author_username or author_username.casefold() == tracked):
            name, avatar = author_name, author_avatar
        else:
            # The tracker username remains correct even if a transient upstream
            # response omits the account profile.
            name, avatar = tracked_username, None

    suffix = f' | {WEBHOOK_SUFFIX}'
    return f'{name}{suffix}'[:80], _large_avatar(avatar)


def _extension(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    path_match = re.search(r'\.([a-z0-9]{2,5})$', parsed.path, flags=re.IGNORECASE)
    if path_match:
        return path_match.group(1).lower()
    image_format = parse_qs(parsed.query).get('format', [None])[0]
    return (image_format or fallback).lower()


def media_candidates(item: Any, position: int) -> list[MediaCandidate]:
    """Extract direct photo/MP4 candidates from FxEmbed's media schema."""
    media_type = str(_get(item, 'type', '')).lower()
    item_url = _get(item, 'url')
    is_video = media_type in {'video', 'gif', 'animated_gif'}
    candidates: list[MediaCandidate] = []

    if is_video:
        for variant in _get(item, 'formats', []) or []:
            container = str(_get(variant, 'container', '')).lower()
            url = _get(variant, 'url')
            if url and container == 'mp4':
                candidates.append(MediaCandidate(
                    url=url,
                    filename=f'tweet-video-{position}.mp4',
                    size=_as_positive_int(_get(variant, 'size') or _get(item, 'filesize')),
                    bitrate=_as_positive_int(_get(variant, 'bitrate')),
                ))
        if not candidates and item_url:
            candidates.append(MediaCandidate(
                url=item_url,
                filename=f'tweet-video-{position}.{_extension(item_url, "mp4")}',
                size=_as_positive_int(_get(item, 'filesize')),
            ))
    elif item_url:
        candidates.append(MediaCandidate(
            url=item_url,
            filename=f'tweet-photo-{position}.{_extension(item_url, "jpg")}',
            size=_as_positive_int(_get(item, 'filesize')),
        ))

    return candidates


def _ordered_candidates(candidates: list[MediaCandidate], limit: int) -> list[MediaCandidate]:
    known_fit = [candidate for candidate in candidates if candidate.size is not None and candidate.size <= limit]
    unknown = [candidate for candidate in candidates if candidate.size is None]
    # Prefer the best known quality that Discord can accept. Unknown-size variants
    # are attempted after that and are capped while streaming.
    return (
        sorted(known_fit, key=lambda candidate: (candidate.size or 0, candidate.bitrate or 0), reverse=True)
        + sorted(unknown, key=lambda candidate: candidate.bitrate or 0, reverse=True)
    )


async def _download_file(session: aiohttp.ClientSession, candidate: MediaCandidate, limit: int) -> discord.File | None:
    if candidate.size is not None and candidate.size > limit:
        return None

    try:
        async with session.get(candidate.url, allow_redirects=True, headers={'User-Agent': 'Personal-TweetCord/1.0'}) as response:
            if response.status != 200:
                log.warning(f'failed to download media (status {response.status})')
                return None

            content_length = response.content_length
            if content_length is not None and content_length > limit:
                return None

            payload = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                payload.extend(chunk)
                if len(payload) > limit:
                    return None

            if not payload:
                return None

            return discord.File(io.BytesIO(payload), filename=candidate.filename)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        log.warning(f'failed to download media: {error}')
    except Exception as error:
        log.warning(f'unexpected media download error: {error}')
    return None


async def prepare_media_delivery(session: aiohttp.ClientSession, parsed_tweet: ParsedTweet | None, upload_limit: int) -> MediaDelivery:
    """Download tweet media that Discord can host and retain direct fallbacks."""
    if not parsed_tweet or not parsed_tweet.media.items or upload_limit <= 0:
        return MediaDelivery(files=[], fallback_urls=[])

    files: list[discord.File] = []
    fallbacks: list[str] = []
    for position, item in enumerate(parsed_tweet.media.items[:MAX_ATTACHMENTS], start=1):
        candidates = media_candidates(item, position)
        if not candidates:
            continue

        uploaded = None
        for candidate in _ordered_candidates(candidates, upload_limit):
            uploaded = await _download_file(session, candidate, upload_limit)
            if uploaded:
                files.append(uploaded)
                break

        if not uploaded:
            # This is a direct media URL (normally video.twimg.com), not the X post.
            # It lets the user open the media even when the channel's upload limit is too low.
            fallbacks.append(candidates[0].url)

    return MediaDelivery(files=files, fallback_urls=list(dict.fromkeys(fallbacks)))


class TweetDelivery:
    """Send notifications through a per-channel webhook when permissions allow it."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._webhooks: dict[int, discord.Webhook] = {}

    @staticmethod
    def _webhook_channel(channel: discord.abc.Messageable) -> Any:
        return channel.parent if isinstance(channel, discord.Thread) else channel

    async def _get_webhook(self, channel: discord.abc.Messageable) -> discord.Webhook | None:
        webhook_channel = self._webhook_channel(channel)
        channel_id = getattr(webhook_channel, 'id', None)
        if channel_id in self._webhooks:
            return self._webhooks[channel_id]

        try:
            me = getattr(channel.guild, 'me', None)
            if me and not webhook_channel.permissions_for(me).manage_webhooks:
                log.warning(f'cannot use account identity in #{getattr(webhook_channel, "name", channel_id)}: grant Manage Webhooks to the bot')
                return None

            webhooks = await webhook_channel.webhooks()
            webhook = discord.utils.find(lambda hook: hook.name == WEBHOOK_NAME, webhooks)
            if webhook is None:
                webhook = await webhook_channel.create_webhook(name=WEBHOOK_NAME, reason='Personal TweetCord per-account delivery')
            self._webhooks[channel_id] = webhook
            return webhook
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(f'cannot create or read TweetCord webhook for channel {channel_id}: {error}')
            return None

    async def send(self, channel: discord.abc.Messageable, *, content: str, username: str, avatar_url: str | None, embeds: list[discord.Embed] | None = None, files: list[discord.File] | None = None, view: discord.ui.View | None = None, suppress_embeds: bool = True) -> None:
        files = files or []
        webhook = await self._get_webhook(channel)
        if webhook:
            kwargs: dict[str, Any] = {
                'content': content,
                'embeds': embeds or [],
                'files': files,
                'view': view,
                'username': username,
                'suppress_embeds': suppress_embeds,
            }
            if avatar_url:
                kwargs['avatar_url'] = avatar_url
            if isinstance(channel, discord.Thread):
                kwargs['thread'] = channel
            try:
                await webhook.send(**kwargs)
                return
            except (discord.Forbidden, discord.HTTPException) as error:
                self._webhooks.pop(getattr(self._webhook_channel(channel), 'id', None), None)
                log.warning(f'webhook delivery failed; falling back to the bot account: {error}')

        for file in files:
            file.reset()
        await channel.send(content, embeds=embeds or [], files=files, view=view, suppress_embeds=suppress_embeds)
