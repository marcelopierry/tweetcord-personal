import asyncio
import os
import re
import aiohttp
from datetime import datetime, timezone, timedelta

import aiosqlite
import discord
from discord.ext import commands
from tweety import Twitter

from core.classes import ParsedTweet
from configs.load_configs import configs, IS_TRANSLATION_ENABLED
from src.i18n import t
from src.log import setup_logger
from src.notification.display_tools import get_action
from src.notification.delivery import ChannelDeliverySequencer, TweetDelivery, build_delivery_links, build_delivery_text, build_quote_original_embed, build_tweet_embed, build_webhook_identity, extract_video_urls, get_delivery_references, prepare_media_delivery
from src.notification.delivery_history import DeliveryHistory
from src.notification.channel_filter import GIANTS_CHANNEL_FILTER
from src.notification.get_tweets import get_tweets
from src.notification.date_comparator import date_comparator
from src.notification.x_auth import authenticate_x, recover_x_account
from src.notification.delay_queue import DelayedTweetBuffer
from src.notification.server_delay import ServerDelays
from src.notification.server_delivery import ServerDelivery
from src.notification.x_validation import TweetSuperseded, validate_on_x
from src.notification.subscriptions import ensure_subscription
from src.notification.x_connection import XConnection
from src.notification.utils import TweetRefreshError, TweetUnavailable, fetch_fresh_parsed_tweet, is_match_media_type, is_match_type, replace_emoji
from src.utils import get_accounts, get_lock, get_utcnow
from src.db_function.readonly_db import connect_readonly
from src.db_function.init_db import init_latest_tweet_on_startup

EMBED_TYPE: str = configs['embed']['type']
SERVICE: str = configs['embed']['proxy']['service']
DOMAIN_NAME: str = configs['embed']['proxy']['domain_name']
DELETION_CONFIRMATIONS = 2
VALIDATION_RETRY_SECONDS = 60

log = setup_logger(__name__)
lock = get_lock()

class AccountTracker():
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.accounts_data = get_accounts()
        self.db_path = os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')
        self.tweets = {account_name: [] for account_name in self.accounts_data.keys()}
        self.pending_tweets: dict[tuple[str, str, str], DelayedTweetBuffer] = {}
        self.ready_tweets = {}
        self.unavailable_checks: dict[tuple[str, str, str], int] = {}
        self.server_delays = ServerDelays(configs.get('notification_delay_seconds', 180))
        self.server_delivery = ServerDelivery()
        self.server_command_locks = {}
        self.session = None
        self.x_clients = {}
        self.feed_checked_at = {}
        self.x_validation_lock = asyncio.Lock()
        self.delivery = TweetDelivery(bot)
        self.delivery_sequencer = ChannelDeliverySequencer()
        self.delivery_history = DeliveryHistory(self.db_path)
        # Responsible for processing queries and writing timestamps
        self.db_write_queue = asyncio.Queue()
        self.latest_tweet_timestamps = {}
        self.timestamps_ready = asyncio.Event()

        self.tasksMonitorLogAt = datetime.now(timezone.utc) - timedelta(hours=configs['tasks_monitor_log_period'])
        bot.loop.create_task(self.setup_tasks())

    async def load_notification_delay(self) -> None:
        async with lock:
            await self.server_delays.load(self.db_path, [guild.id for guild in self.bot.guilds])

    async def set_notification_delay(self, server_id: int, delay_seconds: int) -> int:
        """Persist and reschedule only this server's pending notifications."""
        delay_seconds = max(0, int(delay_seconds))
        async with lock:
            await self.server_delays.set(self.db_path, server_id, delay_seconds)
        queued = 0
        for key, pending in self.pending_tweets.items():
            if key[2] != str(server_id):
                continue
            pending.set_delay_seconds(delay_seconds)
            queued += len(pending)
        log.info(f'server {server_id} notification delay changed to {delay_seconds} seconds; updated {queued} queued tweets')
        return queued

    async def set_server_enabled(self, server_id: int, enabled: bool) -> bool:
        command_lock = self.server_command_locks.setdefault(str(server_id), asyncio.Lock())
        async with command_lock:
            async with lock:
                # Commands can arrive just as startup is loading persisted state.
                await self.server_delivery.load(self.db_path)
                changed = await self.server_delivery.set_enabled(self.db_path, server_id, enabled)
            if changed:
                for collection in (self.pending_tweets, self.ready_tweets):
                    for key in list(collection):
                        if key[2] == str(server_id):
                            collection.pop(key, None)
            if not enabled:
                # Finish any tweet already in flight before confirming suspension.
                await self.server_delivery.drain(server_id)
            log.info(f'server {server_id} tweet delivery enabled={enabled}')
            return changed

    def pending_batches(self, username, client_used, candidates, server_ids, now=None):
        """Queue each discovered post independently for every destination server."""
        now = now or datetime.now(timezone.utc)
        server_ids = {str(value) for value in server_ids}
        server_ids = {value for value in server_ids if self.server_delivery.enabled(value)}
        prefix = (client_used, username)
        for collection in (self.pending_tweets, self.ready_tweets):
            for key in list(collection):
                if key[:2] == prefix and key[2] not in server_ids:
                    collection.pop(key, None)
        batches = []
        for server_id in sorted(server_ids):
            key = (*prefix, server_id)
            pending = self.pending_tweets.setdefault(key, DelayedTweetBuffer(self.server_delays.get(server_id)))
            for tweet in candidates or []:
                if self.server_delivery.allows(server_id, tweet):
                    pending.add(tweet, now)
            ready = pending.pop_ready(now)
            self.ready_tweets[key] = ready
            if ready:
                batches.append((server_id, pending, ready))
        return batches

    async def notification_settings(self, username):
        async with connect_readonly(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Always join the server, regardless of translation settings.
            rows = await (await db.execute('''
                SELECT n.*, c.server_id, suc.translate AS server_translate
                FROM notification n JOIN user u ON u.id=n.user_id
                JOIN channel c ON c.id=n.channel_id
                LEFT JOIN server_user_config suc ON suc.server_id=c.server_id AND suc.user_id=n.user_id
                WHERE u.username=? AND u.enabled=1 AND n.enabled=1
            ''', (username,))).fetchall()
        by_server = {}
        for row in rows:
            by_server.setdefault(str(row['server_id']), []).append(row)
        return by_server

    async def setup_tasks(self):
        await self.bot.wait_until_ready()
        self.session = aiohttp.ClientSession()
        await self.load_notification_delay()
        async with lock:
            await self.server_delivery.load(self.db_path)
        if configs['init_latest_tweet_on_startup']:
            await init_latest_tweet_on_startup(self.db_path)

        # Start the core database workers first
        self.bot.loop.create_task(self.timestamp_updater()).set_name('TimestampUpdater')
        self.bot.loop.create_task(self.db_writer()).set_name('DBWriter')

        # Wait for the initial timestamp load
        await self.timestamps_ready.wait()

        async def start_account(account_name, account_token):
            app = await recover_x_account(account_name, account_token)
            async def reconnect():
                return await authenticate_x(account_name, account_token)
            app = XConnection(app, reconnect)
            self.x_clients[account_name] = app
            self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(f'TweetsUpdater_{account_name}')

        for account_name, account_token in self.accounts_data.items():
            self.bot.loop.create_task(start_account(account_name, account_token)).set_name(f'XAuthentication_{account_name}')

        # Initial user list for notification tasks
        for (username, client_used), _ in self.latest_tweet_timestamps.items():
            self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        
        self.bot.loop.create_task(self.tasksMonitor()).set_name('TasksMonitor')
        self.bot.loop.create_task(self.subscriptionMonitor()).set_name('SubscriptionMonitor')

    async def subscriptionMonitor(self):
        """Reconcile active subscriptions at startup and every thirty minutes."""
        while True:
            if not self.x_clients:
                await asyncio.sleep(5)
                continue
            checked = repaired = failed = 0
            try:
                async with connect_readonly(self.db_path) as db:
                    rows = await (await db.execute(
                        'SELECT id, username, client_used FROM user WHERE enabled = 1 '
                        'AND EXISTS (SELECT 1 FROM notification n WHERE n.user_id = user.id AND n.enabled = 1)'
                    )).fetchall()
                for user_id, username, account in rows:
                    try:
                        # Recheck membership so removals during the sweep stay removed.
                        if (username, account) not in self.latest_tweet_timestamps:
                            continue
                        if account not in self.x_clients:
                            continue
                        changed = await ensure_subscription(self.x_clients[account], user_id)
                        checked += 1
                        if changed:
                            repaired += 1
                            log.warning(f'repaired X subscription drift for {username} via {account}')
                    except Exception as error:
                        failed += 1
                        log.error(f'X subscription verification failed for {username}: {type(error).__name__}')
                    await asyncio.sleep(2)
                log.info(f'X subscription audit: verified={checked}, repaired={repaired}, failed={failed}')
            except Exception as error:
                log.error(f'X subscription audit failed: {type(error).__name__}')
            await asyncio.sleep(1800)

    async def timestamp_updater(self):
        """Periodically reads all user timestamps from the DB into a shared dictionary."""
        while True:
            try:
                async with connect_readonly(self.db_path) as db:
                    async with db.execute('SELECT username, client_used, latest_tweet FROM user WHERE enabled = 1') as cursor:
                        new_timestamps = {}
                        async for row in cursor:
                            new_timestamps[(row[0], row[1])] = row[2]
                        self.latest_tweet_timestamps = new_timestamps
                
                if not self.timestamps_ready.is_set():
                    self.timestamps_ready.set()
                    log.info("initial tweet timestamps loaded")

            except Exception as e:
                log.error(f"error in timestamp_updater: {e}")

            # After careful consideration, it was decided to keep it hard-coded, as it makes little sense to allow users to customize this value.
            await asyncio.sleep(60)

    async def db_writer(self):
        """Singleton task to handle all database write operations."""
        while True:
            try:
                username, new_timestamp = await self.db_write_queue.get()
                async with lock:
                    async with aiosqlite.connect(self.db_path, timeout=10) as db:
                        await db.execute('UPDATE user SET latest_tweet = ? WHERE username = ?', (str(new_timestamp), username))
                        await db.commit()
                self.db_write_queue.task_done()
            except Exception as e:
                log.error(f"error in db_writer: {e}")

    async def _deliver_tweet_to_channel(
        self,
        username: str,
        tweet,
        data,
        channel,
        parsed_tweet: ParsedTweet | None,
        view: discord.ui.View | None,
        lang: str | None,
    ) -> None:
        """Send every component of one tweet while holding its channel lock."""
        blocked_term = GIANTS_CHANNEL_FILTER.blocked_reason(channel.id, username, tweet, parsed_tweet)
        if blocked_term:
            log.info(f'filtered tweet {tweet.id} in channel {channel.id}: matched {blocked_term!r}')
            return
        references = get_delivery_references(tweet, parsed_tweet)
        claim_id = references.claim_id
        if not await self.delivery_history.claim(channel.id, claim_id):
            log.info(
                f'skipping already delivered tweet {claim_id} in channel {channel.id} '
                f'(tracked account: {username}, wrapper: {references.tweet_id})'
            )
            return

        try:
            url = tweet.url
            original_url = references.original_url
            if EMBED_TYPE == 'proxy':
                url = url.replace('twitter', DOMAIN_NAME)
                if IS_TRANSLATION_ENABLED:
                    url += f'/{lang}'
                if original_url:
                    original_url = original_url.replace('twitter', DOMAIN_NAME)
                    if IS_TRANSLATION_ENABLED:
                        original_url += f'/{lang}'

            mention = f"{channel.guild.get_role(int(data['role_id'])).mention} " if data['role_id'] else ''
            author, action = tweet.author.name, get_action(tweet)
            tweet_text = build_delivery_text(tweet, parsed_tweet)
            links = build_delivery_links(
                url,
                parsed_tweet,
                tweet.is_quoted,
                is_retweet=tweet.is_retweet,
                original_url=original_url,
            )
            tweet_embed = build_tweet_embed(username, tweet, parsed_tweet, tweet_text)
            original_embed = build_quote_original_embed(tweet, parsed_tweet)
            tweet_embeds = [embed for embed in (tweet_embed, original_embed) if embed]
            preview_text = '\n'.join(
                part for part in (
                    tweet_text,
                    original_embed.description if original_embed else None,
                )
                if part
            )
            video_urls = extract_video_urls(preview_text)

            message_parts = []
            if data['customized_msg']:
                custom = re.sub(r":(\w+):", lambda match: replace_emoji(match, channel.guild), data['customized_msg']) if configs['emoji_auto_format'] else data['customized_msg']
                message_parts.append(custom.format(mention=mention, author=author, action=action, url=url).rstrip())
            elif mention:
                message_parts.append(mention.rstrip())
            if links:
                message_parts.append(links)
            msg = '\n'.join(part for part in message_parts if part)

            upload_limit = int(channel.guild.filesize_limit)
            primary_media = await prepare_media_delivery(
                self.session,
                parsed_tweet.media if EMBED_TYPE == 'built_in' and parsed_tweet else None,
                upload_limit,
                filename_prefix='quote' if tweet.is_quoted and not tweet.is_retweet else 'tweet',
            )
            original_media = await prepare_media_delivery(
                self.session,
                parsed_tweet.quote_media if EMBED_TYPE == 'built_in' and parsed_tweet and tweet.is_quoted else None,
                upload_limit,
                filename_prefix='original',
            )

            webhook_name, avatar_url = build_webhook_identity(username, tweet, parsed_tweet)
            await self.delivery.send(
                channel,
                content=msg,
                username=webhook_name,
                avatar_url=avatar_url,
                embeds=tweet_embeds or None,
                view=view,
                suppress_embeds=False if tweet_embeds else True,
            )
        except Exception as error:
            await self.delivery_history.release(channel.id, claim_id)
            log.error(f'an error occurred at {channel.mention} while sending notification: {error}')
            return

        # Send main/quote media first, then the quoted original's media. Keeping
        # these as separate messages preserves both grouping and visual order.
        for label, media in (('tweet', primary_media), ('quoted original', original_media)):
            if not media.files and not media.fallback_urls:
                continue
            media_content = '▷'
            if media.fallback_urls:
                media_content += '\n' + '\n'.join(media.fallback_urls)
            try:
                await self.delivery.send(
                    channel,
                    content=media_content,
                    username=webhook_name,
                    avatar_url=avatar_url,
                    files=media.files,
                    suppress_embeds=not media.fallback_urls,
                )
            except Exception as error:
                log.warning(f'failed to send {label} media follow-up for {tweet.url}: {error}')

        # Only known video links receive separate native previews. Article and
        # general website links stay solely inside the text card.
        for video_url in video_urls:
            try:
                await self.delivery.send(
                    channel,
                    content=f'↧\n{video_url}',
                    username=webhook_name,
                    avatar_url=avatar_url,
                    suppress_embeds=False,
                )
            except Exception as error:
                log.warning(f'failed to send video-link preview for {video_url}: {error}')

        try:
            await self.delivery_history.record(channel.id, *references.record_ids)
        except Exception as error:
            log.error(f'failed to update delivery history for channel {channel.id}: {error}')

    async def _refresh_ready_tweet(
        self,
        pending: DelayedTweetBuffer,
        tweet,
        username: str,
        client_used: str,
        server_id: str | None = None,
    ) -> tuple[str, ParsedTweet | None]:
        """Require a current upstream representation before a delayed send."""
        tweet_key = str(getattr(tweet, 'id', None) or getattr(tweet, 'url', ''))
        validation_key = (client_used, username, tweet_key)
        if server_id is not None:
            validation_key += (str(server_id),)

        try:
            async with self.x_validation_lock:
                await validate_on_x(self.x_clients[client_used], tweet_key)
            parsed_tweet = await fetch_fresh_parsed_tweet(tweet, self.session)
        except TweetSuperseded as error:
            self.unavailable_checks.pop(validation_key, None)
            log.info(f'cancelled superseded tweet before delivery: {error}')
            return 'superseded', None
        except TweetUnavailable:
            confirmations = self.unavailable_checks.get(validation_key, 0) + 1
            self.unavailable_checks[validation_key] = confirmations
            if confirmations < DELETION_CONFIRMATIONS:
                pending.defer(tweet, retry_seconds=VALIDATION_RETRY_SECONDS)
                log.info(
                    f'tweet {tweet.url} is unavailable at delivery time; '
                    f'rechecking before cancellation'
                )
                return 'deferred', None

            self.unavailable_checks.pop(validation_key, None)
            log.info(f'cancelled deleted tweet before delivery: {tweet.url}')
            return 'deleted', None
        except TweetRefreshError as error:
            pending.defer(tweet, retry_seconds=VALIDATION_RETRY_SECONDS)
            log.warning(f'deferred tweet because final validation failed for {tweet.url}: {error}')
            return 'deferred', None

        self.unavailable_checks.pop(validation_key, None)
        return 'available', parsed_tweet

    async def queued_original_for_channel(self, tweet, parsed_tweet, channel_id, server_id=None):
        if not getattr(tweet, 'is_retweet', False):
            return False
        original_id = get_delivery_references(tweet, parsed_tweet).original_id
        if not original_id:
            return False
        def same_server(key):
            return server_id is None or len(key) < 3 or key[2] == str(server_id)
        pools = [buffer.tweets() for key, buffer in self.pending_tweets.items() if same_server(key)]
        pools.extend(tweets for key, tweets in self.ready_tweets.items() if same_server(key))
        for candidate in (candidate for pool in pools for candidate in pool):
            if getattr(candidate, 'is_retweet', False) or str(getattr(candidate, 'id', '')) != original_id:
                continue
            if GIANTS_CHANNEL_FILTER.blocked_reason(
                channel_id, getattr(candidate.author, 'username', ''), candidate,
            ):
                continue
            async with connect_readonly(self.db_path) as db:
                rows = await (await db.execute(
                    'SELECT n.enable_type, n.enable_media_type FROM notification n '
                    'JOIN user u ON u.id=n.user_id WHERE u.id=? AND u.enabled=1 '
                    'AND n.channel_id=? AND n.enabled=1',
                    (str(candidate.author.id), str(channel_id)),
                )).fetchall()
            media_source = parsed_tweet if parsed_tweet is not None else candidate
            if any(is_match_type(candidate, row[0]) and is_match_media_type(media_source, row[1]) for row in rows):
                return True
        return False

    async def notification(self, username: str, client_used: str):
        while True:
            for key in list(self.ready_tweets):
                if key[:2] == (client_used, username):
                    self.ready_tweets[key] = []
            await asyncio.sleep(configs['tweets_check_period'])

            last_tweet_at = self.latest_tweet_timestamps.get((username, client_used))
            if not last_tweet_at:
                # This can happen if a user is removed right after the sleep.
                log.warning(f"no timestamp for {username}, task will terminate.")
                break

            try:
                notifications_by_server = await self.notification_settings(username)
            except aiosqlite.OperationalError as error:
                log.warning(f'unable to read notification destinations for {username}: {type(error).__name__}')
                continue
            candidates = await get_tweets(self.tweets[client_used], username, last_tweet_at)
            batches = self.pending_batches(username, client_used, candidates, notifications_by_server)
            latest_tweets = [tweet for _, _, tweets in batches for tweet in tweets]
            if not latest_tweets:
                continue
            
            newest_timestamp = max(tweet.created_on for tweet in latest_tweets)
            # Update local cache immediately to prevent re-notification
            if date_comparator(newest_timestamp, last_tweet_at) == 1:
                self.latest_tweet_timestamps[(username, client_used)] = str(newest_timestamp)
                await self.db_write_queue.put((username, newest_timestamp))

            validated_tweets = []
            for server_id, pending, tweets in batches:
                validated = []
                for tweet in tweets:
                    if not self.server_delivery.allows(server_id, tweet):
                        continue
                    status, fresh_parsed_tweet = await self._refresh_ready_tweet(
                        pending, tweet, username, client_used, server_id,
                    )
                    if status == 'available' and fresh_parsed_tweet is not None:
                        validated_tweets.append((server_id, pending, tweet, fresh_parsed_tweet))
                        validated.append(tweet)
                self.ready_tweets[(client_used, username, server_id)] = validated
            if not validated_tweets:
                continue

            for server_id, pending, tweet, fresh_parsed_tweet in validated_tweets:
                log.info(f'find a new tweet from {username}')
                
                content_cache: dict[str, tuple[ParsedTweet | None, discord.ui.View | None]] = {}
                
                def gen_view(label: str, url: str):
                    view = discord.ui.View(timeout=5)
                    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
                    return view
                
                view = None
                if EMBED_TYPE == 'proxy' and configs['embed']['proxy']['original_url_button']:
                    view = gen_view(t('display.button.view_original'), tweet.url)

                for data in notifications_by_server[server_id]:
                    channel = self.bot.get_channel(int(data['channel_id']))
                    if channel is None or not is_match_type(tweet, data['enable_type']):
                        continue
                    
                    lang = (data['server_translate'] or configs['embed']['trans_default_lang']) if IS_TRANSLATION_ENABLED else None
                    
                    if lang not in content_cache:
                        p_tweet = None
                        
                        if EMBED_TYPE == 'built_in':
                            p_tweet = fresh_parsed_tweet
                            if lang:
                                try:
                                    p_tweet = await fetch_fresh_parsed_tweet(tweet, self.session, lang=lang)
                                except (TweetUnavailable, TweetRefreshError) as error:
                                    log.warning(
                                        f'unable to refresh translated tweet {tweet.url}: {error}; '
                                        f'using the validated original language'
                                    )
                            if view is None and p_tweet.media.type == 'video' and configs['embed']['built_in']['video_link_button']:
                                button_url = p_tweet.media.video_link or tweet.url
                                view = gen_view(t('display.button.view_video'), button_url)
                        
                        content_cache[lang] = (p_tweet, view)

                    current_p_tweet, current_view = content_cache[lang]

                    if not is_match_media_type(current_p_tweet if current_p_tweet else tweet, data['enable_media_type']):
                        continue

                    # Different account tasks may target the same channel at the
                    # same moment. Hold the channel lock until links, cards, both
                    # media groups, previews, and history are all complete.
                    async with self.delivery_sequencer.lock_for(channel.id):
                        if await self.queued_original_for_channel(tweet, current_p_tweet, channel.id, server_id):
                            pending.defer(tweet, retry_seconds=60)
                            log.info(f'deferred retweet {tweet.id} in channel {channel.id}: queued original has priority')
                            continue
                        async with self.server_delivery.delivery(server_id, tweet) as allowed:
                            if allowed:
                                await self._deliver_tweet_to_channel(
                                    username,
                                    tweet,
                                    data,
                                    channel,
                                    current_p_tweet,
                                    current_view,
                                    lang,
                                )

    async def tweetsUpdater(self, app: Twitter):
        updater_name = asyncio.current_task().get_name().split('_', 1)[1]
        while True:
            try:
                # Tweety is async when called on this loop. Never move its shared
                # httpx.AsyncClient into a worker thread/new event loop.
                self.tweets[updater_name] = await asyncio.wait_for(app.get_tweet_notifications(), 60)
                self.feed_checked_at[updater_name] = datetime.now(timezone.utc)
                log.info(f'X feed poll completed for {updater_name}: {len(self.tweets[updater_name])} posts')
            except KeyError as e:
                # Handle the error thrown by `tweety-ns` mentioned in issue#59
                log.warning(f"handled KeyError in {updater_name}: {e}. This is likely a temporary API response issue from Twitter. Skipping this check.")
            except Exception as e:
                log.error(f'X feed poll failed for {updater_name}: {type(e).__name__}; retrying in 60 seconds')
                await asyncio.sleep(60)
                continue
            
            await asyncio.sleep(configs['tweets_check_period'])

    async def tasksMonitor(self):
        """Dynamically monitors tasks based on the live timestamp cache."""
        while True:
            await asyncio.sleep(configs['tasks_monitor_check_period'] * 60)

            running_tasks = {task.get_name() for task in asyncio.all_tasks()}
            users_in_cache = {username for username, _ in self.latest_tweet_timestamps.keys()}
            
            alive_tasks = running_tasks & users_in_cache

            if alive_tasks != users_in_cache:
                dead_tasks = list(users_in_cache - alive_tasks)
                if dead_tasks:
                    log.warning(f'dead tasks : {dead_tasks}')
                    for dead_task_username in dead_tasks:
                        # Find the corresponding client_used from the cache
                        client_used = None
                        for u, c in self.latest_tweet_timestamps.keys():
                            if u == dead_task_username:
                                client_used = c
                                break
                        
                        if client_used:
                            self.bot.loop.create_task(self.notification(dead_task_username, client_used)).set_name(dead_task_username)
                            log.info(f'restart {dead_task_username} successfully using {client_used}')

            for client in self.accounts_data.keys():
                last_poll = self.feed_checked_at.get(client)
                if last_poll is None or (datetime.now(timezone.utc) - last_poll).total_seconds() > 180:
                    log.error(f'X feed {client} has no successful poll within three minutes')
                if f'TweetsUpdater_{client}' not in running_tasks:
                    log.warning(f'tweets updater {client} : dead')

            if (datetime.now(timezone.utc) - self.tasksMonitorLogAt).total_seconds() / 3600 >= configs['tasks_monitor_log_period']:
                log.info(f'alive tasks : {list(alive_tasks)}')
                for client in self.accounts_data.keys():
                    if f'TweetsUpdater_{client}' in running_tasks:
                        log.info(f'tweets updater {client} : alive')
                self.tasksMonitorLogAt = datetime.now(timezone.utc)


    async def addTask(self, username: str, client_used: str):
        """Adds a new user to the live cache and starts their notification task."""
        # Add to live cache first
        self.latest_tweet_timestamps[(username, client_used)] = get_utcnow()
        
        # Start the task
        self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        log.info(f'new task {username} added successfully using {client_used}')

    async def removeTask(self, username: str):
        """Removes a user from the live cache and cancels their notification task."""
        key_to_remove = None
        # Create a copy of keys for safe iteration
        for u, c in list(self.latest_tweet_timestamps.keys()):
            if u == username:
                key_to_remove = (u, c)
                break
        
        # Remove from cache so the monitor doesn't restart it
        if key_to_remove and key_to_remove in self.latest_tweet_timestamps:
            del self.latest_tweet_timestamps[key_to_remove]
            for collection in (self.pending_tweets, self.ready_tweets):
                for key in list(collection):
                    if key[:2] == (key_to_remove[1], username):
                        collection.pop(key, None)

        # Cancel the running task
        for task in asyncio.all_tasks():
            if task.get_name() == username:
                task.cancel()
                log.info(f'task {username} has been cancelled')
                break

    async def close(self):
        """Closes the persistent session."""
        if self.session:
            await self.session.close()
            log.info("account tracker session closed")
