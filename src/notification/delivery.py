from __future__ import annotations

import asyncio
import html
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
from src.utils import escape_markdown, safe_truncate


log = setup_logger(__name__)

WEBHOOK_NAME = 'Personal TweetCord delivery'
WEBHOOK_SUFFIX = 'Personal TweetCord'
MAX_ATTACHMENTS = 10
QUOTE_ORIGINAL_EMOJI = '🔂'
RETWEET_EMOJI = '🔄'
MARKDOWN_URL_RE = re.compile(
    r'\]\((https?://(?:[^\s()]|\([^\s()]*\))+?)\)',
    flags=re.IGNORECASE,
)
BARE_URL_RE = re.compile(r'https?://[^\s<>\]]+', flags=re.IGNORECASE)
X_HOSTS = {'x.com', 'twitter.com'}
VIDEO_HOSTS = {
    'youtube.com',
    'youtu.be',
    'vimeo.com',
    'streamable.com',
    'dailymotion.com',
    'dai.ly',
    'twitch.tv',
    'tiktok.com',
}
VIDEO_EXTENSIONS = ('.mp4', '.webm', '.mov', '.m4v')


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


@dataclass(frozen=True)
class DeliveryReferences:
    claim_id: str
    tweet_id: str
    original_id: str | None
    original_url: str | None
    record_ids: tuple[str, ...]


class ChannelDeliverySequencer:
    """Provide one lock per Discord channel so multipart posts cannot interleave."""

    def __init__(self):
        self._locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, channel_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(channel_id), asyncio.Lock())


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


def build_delivery_links(
    tweet_url: str,
    parsed_tweet: ParsedTweet | None,
    is_quote: bool,
    is_retweet: bool = False,
    original_url: str | None = None,
) -> str:
    """Build clean Discord links for ordinary, quote, and retweeted posts."""
    quote_url = original_url or (parsed_tweet.quote.url if parsed_tweet and parsed_tweet.quote else None)
    if is_retweet:
        if original_url:
            return f'<{tweet_url}>\n{RETWEET_EMOJI} <{original_url}>'
        return f'<{tweet_url}>' if tweet_url else ''
    if is_quote and quote_url:
        # The tracked account's quote post is the new content, so it comes first.
        return f'<{tweet_url}>\n{QUOTE_ORIGINAL_EMOJI} <{quote_url}>'
    return f'<{tweet_url}>' if tweet_url else ''


def build_delivery_text(tweet: Any, parsed_tweet: ParsedTweet | None) -> str:
    """Return the tweet body as message text, capped by ParsedTweet's safe limit."""
    if parsed_tweet:
        parsed_text = parsed_tweet.get_text(simplified_content=True)
        if isinstance(parsed_text, tuple):
            parsed_text = parsed_text[0]
        if parsed_text:
            return str(parsed_text).strip()

    fallback = escape_markdown(html.unescape(str(getattr(tweet, 'text', '') or ''))).strip()
    return safe_truncate(fallback, ParsedTweet.MAX_DESCRIPTION_LENGTH)[0] if fallback else ''


def _clean_preview_url(url: str, *, from_markdown: bool = False) -> str:
    url = url.rstrip('.,!?;:\'"’')
    return url if from_markdown else url.rstrip(')]}')


def _is_x_url(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower().removeprefix('www.')
    return any(host == x_host or host.endswith(f'.{x_host}') for x_host in X_HOSTS)


def extract_external_urls(text: str) -> list[str]:
    """Return unique non-X links from plain text and Discord Markdown links."""
    markdown_matches = list(MARKDOWN_URL_RE.finditer(text or ''))
    markdown_spans = [(match.start(1), match.end(1)) for match in markdown_matches]
    candidates = [
        (match.start(1), match.group(1), True)
        for match in markdown_matches
    ]
    candidates.extend(
        (match.start(), match.group(0), False)
        for match in BARE_URL_RE.finditer(text or '')
        if not any(start <= match.start() < end for start, end in markdown_spans)
    )

    urls: list[str] = []
    seen: set[str] = set()
    for _, candidate, from_markdown in sorted(candidates, key=lambda item: item[0]):
        url = _clean_preview_url(candidate, from_markdown=from_markdown)
        if not url or _is_x_url(url) or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def extract_youtube_urls(text: str) -> list[str]:
    """Backward-compatible helper retained for callers that only want YouTube."""
    youtube_hosts = {'youtube.com', 'youtu.be'}
    return [
        url for url in extract_external_urls(text)
        if any(
            (urlparse(url).hostname or '').lower().removeprefix('www.').removeprefix('m.') == host
            for host in youtube_hosts
        )
    ]


def extract_video_urls(text: str) -> list[str]:
    """Return links that are known video pages or direct playable video files."""
    video_urls: list[str] = []
    for url in extract_external_urls(text):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().removeprefix('www.').removeprefix('m.')
        is_video_host = any(host == video_host or host.endswith(f'.{video_host}') for video_host in VIDEO_HOSTS)
        if is_video_host or parsed.path.lower().endswith(VIDEO_EXTENSIONS):
            video_urls.append(url)
    return video_urls


def get_delivery_references(tweet: Any, parsed_tweet: ParsedTweet | None) -> DeliveryReferences:
    """Identify what a channel is seeing for persistent deduplication."""
    tweet_id = str(getattr(tweet, 'id', None) or getattr(tweet, 'url', ''))
    is_retweet = bool(getattr(tweet, 'is_retweet', False))
    is_quote = bool(getattr(tweet, 'is_quoted', False))
    source = getattr(tweet, 'retweeted_tweet', None) if is_retweet else getattr(tweet, 'quoted_tweet', None)
    original_url = getattr(source, 'url', None)
    if is_retweet and not original_url and parsed_tweet:
        # Tweety notifications sometimes identify a post as a retweet without
        # hydrating retweeted_tweet. FxTwitter still returns the canonical
        # original URL and author alongside reposted_by, so use that source.
        original_url = getattr(parsed_tweet, 'source_url', None)
    if is_quote and not original_url and parsed_tweet:
        original_url = parsed_tweet.quote.url
    original_id = getattr(source, 'id', None)
    if original_id is None and is_retweet and parsed_tweet:
        original_id = getattr(parsed_tweet, 'source_id', None)
    if original_id is None and original_url:
        match = re.search(r'/status/(\d+)', original_url)
        original_id = match.group(1) if match else None
    original_id = str(original_id) if original_id is not None else None

    post_key = f'post:{tweet_id}'
    original_key = f'original:{original_id or tweet_id}'

    # Namespaces distinguish a quote wrapper from an original delivered directly
    # or by retweet. They also prevent ambiguous legacy context rows from causing
    # new false skips after this behavior change.
    claim_id = original_key if is_retweet or not is_quote else post_key
    if is_retweet:
        record_ids = tuple(dict.fromkeys((post_key, original_key)))
    elif is_quote:
        record_ids = (post_key,)
    else:
        record_ids = (original_key,)
    return DeliveryReferences(claim_id, tweet_id, original_id, original_url, record_ids)


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


def build_tweet_embed(
    tracked_username: str,
    tweet: Any,
    parsed_tweet: ParsedTweet | None,
    tweet_text: str,
) -> discord.Embed | None:
    """Render the tweet body in a compact Discord card without duplicating media."""
    if not tweet_text:
        return None

    is_retweet = bool(getattr(tweet, 'is_retweet', False))
    source = getattr(tweet, 'retweeted_tweet', None) if is_retweet else tweet
    source_author = getattr(source, 'author', None)

    if is_retweet:
        display_name = (
            getattr(parsed_tweet, 'author_name', None)
            or getattr(source_author, 'name', None)
            or 'Original author'
        )
        display_username = (
            getattr(parsed_tweet, 'author_username', None)
            or getattr(source_author, 'username', None)
        )
        avatar_url = _large_avatar(
            getattr(parsed_tweet, 'author_avatar_url', None)
            or getattr(source_author, 'profile_image_url_https', None)
        )
    else:
        webhook_name, avatar_url = build_webhook_identity(tracked_username, tweet, parsed_tweet)
        display_name = webhook_name.removesuffix(f' | {WEBHOOK_SUFFIX}')
        display_username = tracked_username

    embed = discord.Embed(
        description=tweet_text,
        color=0x1DA1F2,
        timestamp=getattr(source, 'created_on', None),
    )
    author_name = f'{display_name} (@{display_username})' if display_username else display_name
    author_args: dict[str, Any] = {
        'name': author_name,
        'url': f'https://x.com/{display_username or tracked_username}',
    }
    if avatar_url:
        author_args['icon_url'] = avatar_url
    embed.set_author(**author_args)
    embed.set_footer(text=WEBHOOK_SUFFIX)
    return embed


def build_quote_original_embed(tweet: Any, parsed_tweet: ParsedTweet | None) -> discord.Embed | None:
    """Render a quote tweet's referenced original as a second Discord card."""
    if not getattr(tweet, 'is_quoted', False):
        return None

    source = getattr(tweet, 'quoted_tweet', None)
    quote = getattr(parsed_tweet, 'quote', None)
    quote_text = getattr(quote, 'trans_text', None) or getattr(quote, 'text', None)
    if not quote_text and source:
        quote_text = escape_markdown(html.unescape(str(getattr(source, 'text', '') or '')))
    if not quote_text:
        return None
    quote_text = safe_truncate(str(quote_text).strip(), ParsedTweet.MAX_DESCRIPTION_LENGTH)[0]

    source_author = getattr(source, 'author', None)
    name = getattr(quote, 'name', None) or getattr(source_author, 'name', None) or 'Original tweet'
    username = getattr(quote, 'screen_name', None) or getattr(source_author, 'username', None)
    avatar_url = _large_avatar(
        getattr(quote, 'avatar_url', None)
        or getattr(source_author, 'profile_image_url_https', None)
    )
    source_url = getattr(quote, 'url', None) or getattr(source, 'url', None)

    embed = discord.Embed(
        description=quote_text,
        color=0xAAB8C2,
        timestamp=getattr(source, 'created_on', None),
    )
    author_name = f'{name} (@{username})' if username else name
    author_args: dict[str, Any] = {'name': author_name}
    if source_url:
        author_args['url'] = source_url
    if avatar_url:
        author_args['icon_url'] = avatar_url
    embed.set_author(**author_args)
    embed.set_footer(text=WEBHOOK_SUFFIX)
    return embed


def _extension(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    path_match = re.search(r'\.([a-z0-9]{2,5})$', parsed.path, flags=re.IGNORECASE)
    if path_match:
        return path_match.group(1).lower()
    image_format = parse_qs(parsed.query).get('format', [None])[0]
    return (image_format or fallback).lower()


def media_candidates(item: Any, position: int, filename_prefix: str = 'tweet') -> list[MediaCandidate]:
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
                    filename=f'{filename_prefix}-video-{position}.mp4',
                    size=_as_positive_int(_get(variant, 'size') or _get(item, 'filesize')),
                    bitrate=_as_positive_int(_get(variant, 'bitrate')),
                ))
        if not candidates and item_url:
            candidates.append(MediaCandidate(
                url=item_url,
                filename=f'{filename_prefix}-video-{position}.{_extension(item_url, "mp4")}',
                size=_as_positive_int(_get(item, 'filesize')),
            ))
    elif item_url:
        candidates.append(MediaCandidate(
            url=item_url,
            filename=f'{filename_prefix}-photo-{position}.{_extension(item_url, "jpg")}',
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


async def prepare_media_delivery(
    session: aiohttp.ClientSession,
    media_source: ParsedTweet | ParsedTweet.Media | None,
    upload_limit: int,
    filename_prefix: str = 'tweet',
) -> MediaDelivery:
    """Download tweet media that Discord can host and retain direct fallbacks."""
    media = media_source.media if isinstance(media_source, ParsedTweet) else media_source
    if not media or not media.items or upload_limit <= 0:
        return MediaDelivery(files=[], fallback_urls=[])

    files: list[discord.File] = []
    fallbacks: list[str] = []
    for position, item in enumerate(media.items[:MAX_ATTACHMENTS], start=1):
        candidates = media_candidates(item, position, filename_prefix)
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
                'username': username,
                'suppress_embeds': suppress_embeds,
                # Discord accepts webhook messages asynchronously unless wait is
                # requested. Without this confirmation a later media request can
                # occasionally be committed before the tweet/link message.
                'wait': True,
            }
            if view is not None:
                kwargs['view'] = view
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
        kwargs = {
            'embeds': embeds or [],
            'files': files,
            'suppress_embeds': suppress_embeds,
        }
        if view is not None:
            kwargs['view'] = view
        await channel.send(content, **kwargs)
