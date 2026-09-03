git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-fe6Pmy9l' (errno=Operation not permitted)
2026-09-02 22:50:59.933 xcodebuild[56268:5959411]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:00.057 xcodebuild[56268:5959410] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp instead
git: error: couldn't create cache file '/tmp/xcrun_db-M6blnBR2' (errno=Operation not permitted)
2026-09-02 22:51:00.480 xcodebuild[56272:5959433]  DVTFilePathFSEvents: Failed to start fs event stream.
2026-09-02 22:51:00.604 xcodebuild[56272:5959432] [MT] DVTDeveloperPaths: Failed to get length of DARWIN_USER_CACHE_DIR from confstr(3), error = Error Domain=NSPOSIXErrorDomain Code=5 "Input/output error". Using NSCachesDirectory instead.
import re
import discord

import aiohttp
from bs4 import BeautifulSoup
from tweety.types import Tweet

from core.classes import ParsedTweet
from configs.load_configs import FX_SETTINGS
from src.log import setup_logger

log = setup_logger(__name__)


class TweetUnavailable(Exception):
    """The tweet no longer exists at delivery time."""


class TweetRefreshError(Exception):
    """The tweet could not be authoritatively refreshed right now."""


def _fx_api_url(tweet: Tweet, lang: str = None) -> str:
    api_url = re.sub(r'(?:twitter|x)\.com', r'api.fxtwitter.com', tweet.url)
    return f'{api_url}/{lang}' if lang else api_url


async def fetch_fresh_parsed_tweet(
    tweet: Tweet,
    session: aiohttp.ClientSession,
    lang: str = None,
) -> ParsedTweet:
    """Fetch the current tweet representation without accepting stale fallbacks."""
    api_url = _fx_api_url(tweet, lang)
    try:
        async with session.get(api_url) as response:
            if response.status in {404, 410}:
                raise TweetUnavailable(tweet.url)
            if response.status != 200:
                raise TweetRefreshError(f'FxTwitter returned status {response.status}')
            data = await response.json()
    except TweetUnavailable:
        raise
    except TweetRefreshError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise TweetRefreshError(str(error)) from error

    if not isinstance(data, dict) or not data.get('tweet'):
        raise TweetUnavailable(tweet.url)
    return ParsedTweet(data)


async def get_parsed_tweet(tweet: Tweet, session: aiohttp.ClientSession = None, lang: str = None) -> ParsedTweet:
    async def get_fx_data(s: aiohttp.ClientSession):
        try:
            return await fetch_fresh_parsed_tweet(tweet, s, lang)
        except TweetUnavailable:
            # Delivery callers use this signal to cancel a deleted post rather
            # than rendering stale notification or HTML data.
            raise
        except TweetRefreshError as error:
            log.warning(f'error fetching fresh FxTwitter data for {tweet.url}: {error}; fallback to HTML scraping')

        html_url = re.sub(r'(?:twitter|x)\.com', r'fxtwitter.com', tweet.url)
        async with s.get(html_url) as response:
            raw = await response.text()
            soup = BeautifulSoup(raw, 'html.parser')
            return ParsedTweet(soup)

    if (
        FX_SETTINGS['media']['enabled']
        or FX_SETTINGS['rt_text']['enabled']
        or FX_SETTINGS['auto_translation']
        or (FX_SETTINGS['mosaic'] and len(tweet.media) > 1)
    ):
        if session:
            return await get_fx_data(session)
        else:
            async with aiohttp.ClientSession() as session_internal:
                return await get_fx_data(session_internal)
    else:
        return ParsedTweet(tweet)


def is_match_type(tweet: Tweet, enable_type: str):
    tweet_type = 0 if tweet.is_retweet else 1 if tweet.is_quoted else -1
    return tweet_type == -1 or enable_type[tweet_type] == '1'


def is_match_media_type(source: Tweet | ParsedTweet, media_type: str):
    if isinstance(source, Tweet): return media_type == '11' or (media_type == '10' and len(source.media) == 0) or (media_type == '01' and len(source.media) > 0)
    elif isinstance(source, ParsedTweet): return media_type == '11' or (media_type == '10' and source.length == 0) or (media_type == '01' and source.length > 0)
    else: raise TypeError('source must be a Tweet or Media')


def replace_emoji(match: re.Match, guild: discord.Guild):
    emoji_name = match.group(1)
    emoji = discord.utils.get(guild.emojis, name=emoji_name)
    return str(emoji) if emoji else match.group(0)
