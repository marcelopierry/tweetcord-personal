"""Check X's current edit/deletion state, which media proxies omit."""
import asyncio

from src.notification.utils import TweetRefreshError, TweetUnavailable


class TweetSuperseded(TweetUnavailable):
    """A newer ID has replaced this version of the post."""


def validate_detail(response, tweet_id):
    tweet_id = str(tweet_id)

    def entries(value):
        if isinstance(value, dict):
            if value.get('entryId') == 'tweet-' + tweet_id:
                yield value
            else:
                for child in value.values():
                    yield from entries(child)
        elif isinstance(value, list):
            for child in value:
                yield from entries(child)

    entry = next(entries(response), None)
    if entry is None:
        # Rate limits, authentication errors, and changed schemas are not deletion.
        raise TweetRefreshError('X lookup did not contain the requested tweet entry')
    item = entry.get('content', {}).get('itemContent', {})
    if item.get('itemType') == 'TimelineTombstone':
        raise TweetUnavailable(tweet_id)
    if 'tweet_results' not in item:
        raise TweetRefreshError('X tweet entry has an unknown shape')
    result = item['tweet_results'].get('result')
    if not result:
        raise TweetUnavailable(tweet_id)
    if result.get('__typename') == 'TweetTombstone':
        raise TweetUnavailable(tweet_id)
    if result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet', {})
    if str(result.get('rest_id')) != tweet_id:
        raise TweetRefreshError('X returned a different tweet ID')
    control = result.get('edit_control', {})
    control = control.get('edit_control_initial', control)
    ids = control.get('edit_tweet_ids')
    if not ids or tweet_id not in [str(value) for value in ids]:
        raise TweetRefreshError('X did not supply usable edit history')
    if str(ids[-1]) != tweet_id:
        raise TweetSuperseded(f'{tweet_id} replaced by {ids[-1]}')


async def validate_on_x(client, tweet_id):
    try:
        response = await asyncio.wait_for(client.request.get_tweet_detail(str(tweet_id)), timeout=30)
    except Exception as error:
        # Do not log authentication response bodies or turn an outage into deletion.
        raise TweetRefreshError(f'X validation failed ({type(error).__name__})') from error
    validate_detail(response, tweet_id)
