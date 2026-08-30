import asyncio
import os
import sys
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
from src.notification.delivery import TweetDelivery, build_delivery_links, build_delivery_text, build_webhook_identity, get_delivery_references, prepare_media_delivery
from src.notification.delivery_history import DeliveryHistory
from src.notification.get_tweets import get_tweets
from src.notification.delay_queue import DelayedTweetBuffer
from src.notification.utils import is_match_media_type, is_match_type, replace_emoji, get_parsed_tweet
from src.utils import get_accounts, get_lock, get_utcnow
from src.db_function.readonly_db import connect_readonly
from src.db_function.init_db import init_latest_tweet_on_startup

EMBED_TYPE: str = configs['embed']['type']
SERVICE: str = configs['embed']['proxy']['service']
DOMAIN_NAME: str = configs['embed']['proxy']['domain_name']

log = setup_logger(__name__)
lock = get_lock()

class AccountTracker():
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.accounts_data = get_accounts()
        self.db_path = os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')
        self.tweets = {account_name: [] for account_name in self.accounts_data.keys()}
        self.pending_tweets: dict[tuple[str, str], DelayedTweetBuffer] = {}
        self.notification_delay_seconds = int(configs.get('notification_delay_seconds', 180))
        self.session = None
        self.delivery = TweetDelivery(bot)
        self.delivery_history = DeliveryHistory(self.db_path)
        # Responsible for processing queries and writing timestamps
        self.db_write_queue = asyncio.Queue()
        self.latest_tweet_timestamps = {}
        self.timestamps_ready = asyncio.Event()

        self.tasksMonitorLogAt = datetime.now(timezone.utc) - timedelta(hours=configs['tasks_monitor_log_period'])
        bot.loop.create_task(self.setup_tasks())

    async def load_notification_delay(self) -> None:
        async with connect_readonly(self.db_path) as db:
            async with db.execute("SELECT value FROM runtime_setting WHERE key = 'notification_delay_seconds'") as cursor:
                row = await cursor.fetchone()
        if row:
            try:
                self.notification_delay_seconds = max(0, int(row[0]))
            except (TypeError, ValueError):
                log.warning('invalid stored notification delay; using configured default')

    async def set_notification_delay(self, delay_seconds: int) -> int:
        """Persist a global delay and shift every queued tweet retroactively."""
        delay_seconds = max(0, int(delay_seconds))
        async with lock:
            async with aiosqlite.connect(self.db_path, timeout=10) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO runtime_setting (key, value) VALUES (?, ?)',
                    ('notification_delay_seconds', str(delay_seconds)),
                )
                await db.commit()

        self.notification_delay_seconds = delay_seconds
        queued = 0
        for pending in self.pending_tweets.values():
            pending.set_delay_seconds(delay_seconds)
            queued += len(pending)
        log.info(f'global notification delay changed to {delay_seconds} seconds; updated {queued} queued tweets')
        return queued

    async def setup_tasks(self):
        self.session = aiohttp.ClientSession()
        await self.load_notification_delay()
        if configs['init_latest_tweet_on_startup']:
            await init_latest_tweet_on_startup(self.db_path)

        # Start the core database workers first
        self.bot.loop.create_task(self.timestamp_updater()).set_name('TimestampUpdater')
        self.bot.loop.create_task(self.db_writer()).set_name('DBWriter')

        # Wait for the initial timestamp load
        await self.timestamps_ready.wait()

        async def authenticate_account(account_name, account_token):
            app = Twitter(account_name)
            max_attempts = configs['auth_max_attempts']
            for attempt in range(max_attempts):
                try:
                    await app.load_auth_token(account_token)
                    return app
                except Exception as e:
                    log.error(f"authentication failed for account: {account_name} [Attempt {attempt + 1}/{max_attempts}]")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(5)
                    else:
                        log.error(f"persistent authentication failure for account {account_name}")
                        raise
        
        for account_name, account_token in self.accounts_data.items():
            try:
                app = await authenticate_account(account_name, account_token)
                self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(f'TweetsUpdater_{account_name}')
            except Exception:
                sys.exit(1)

        # Initial user list for notification tasks
        for (username, client_used), _ in self.latest_tweet_timestamps.items():
            self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        
        self.bot.loop.create_task(self.tasksMonitor()).set_name('TasksMonitor')

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

    async def notification(self, username: str, client_used: str):
        while True:
            await asyncio.sleep(configs['tweets_check_period'])

            last_tweet_at = self.latest_tweet_timestamps.get((username, client_used))
            if not last_tweet_at:
                # This can happen if a user is removed right after the sleep.
                log.warning(f"no timestamp for {username}, task will terminate.")
                break

            pending = self.pending_tweets.setdefault(
                (client_used, username),
                DelayedTweetBuffer(self.notification_delay_seconds),
            )
            candidates = await get_tweets(self.tweets[client_used], username, last_tweet_at)
            if candidates:
                now = datetime.now(timezone.utc)
                for tweet in candidates:
                    pending.add(tweet, now)

            latest_tweets = pending.pop_ready()
            if not latest_tweets:
                continue
            
            newest_timestamp = max(tweet.created_on for tweet in latest_tweets)
            # Update local cache immediately to prevent re-notification
            self.latest_tweet_timestamps[(username, client_used)] = str(newest_timestamp)
            # Queue the database update
            await self.db_write_queue.put((username, newest_timestamp))

            user = None
            notifications = []
            try:
                async with connect_readonly(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.cursor() as cursor:
                        await cursor.execute('SELECT id FROM user WHERE username = ?', (username,))
                        user = await cursor.fetchone()
                        if user:
                            if IS_TRANSLATION_ENABLED:
                                await cursor.execute('''
                                    SELECT n.*, suc.translate AS server_translate
                                    FROM notification n
                                    JOIN channel c ON n.channel_id = c.id
                                    LEFT JOIN server_user_config suc ON c.server_id = suc.server_id AND n.user_id = suc.user_id
                                    WHERE n.user_id = ? AND n.enabled = 1
                                ''', (user['id'],))
                            else:
                                await cursor.execute('SELECT * FROM notification WHERE user_id = ? AND enabled = 1', (user['id'],))
                            notifications = await cursor.fetchall()
            except aiosqlite.OperationalError as e:
                if "database is locked" in str(e):
                    log.warning(f"database locked while reading notification settings for {username}, this is unexpected but handled.")
                else:
                    raise
            
            if not user:
                continue

            for tweet in latest_tweets:
                log.info(f'find a new tweet from {username}')
                
                content_cache: dict[str, tuple[ParsedTweet | None, discord.ui.View | None]] = {}
                
                def gen_view(label: str, url: str):
                    view = discord.ui.View(timeout=5)
                    view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
                    return view
                
                view = None
                if EMBED_TYPE == 'proxy' and configs['embed']['proxy']['original_url_button']:
                    view = gen_view(t('display.button.view_original'), tweet.url)

                for data in notifications:
                    channel = self.bot.get_channel(int(data['channel_id']))
                    if channel is None or not is_match_type(tweet, data['enable_type']):
                        continue
                    
                    lang = (data['server_translate'] or configs['embed']['trans_default_lang']) if IS_TRANSLATION_ENABLED else None
                    
                    if lang not in content_cache:
                        p_tweet = None
                        
                        if EMBED_TYPE == 'built_in':
                            p_tweet = await get_parsed_tweet(tweet, self.session, lang=lang)
                            if view is None and p_tweet.media.type == 'video' and configs['embed']['built_in']['video_link_button']:
                                button_url = p_tweet.media.video_link or tweet.url
                                view = gen_view(t('display.button.view_video'), button_url)
                        
                        content_cache[lang] = (p_tweet, view)

                    current_p_tweet, current_view = content_cache[lang]

                    if not is_match_media_type(current_p_tweet if current_p_tweet else tweet, data['enable_media_type']):
                        continue

                    references = get_delivery_references(tweet, current_p_tweet)
                    original_url = references.original_url

                    # Retweets reserve the original ID, so any original/retweet
                    # already delivered to this channel suppresses the duplicate.
                    # Quotes reserve their own ID and therefore always deliver.
                    claim_id = references.claim_id
                    if not await self.delivery_history.claim(channel.id, claim_id):
                        log.info(f'skipping already delivered tweet {claim_id} in channel {channel.id}')
                        continue

                    try:
                        url = tweet.url
                        if EMBED_TYPE == 'proxy':
                            url = url.replace('twitter', DOMAIN_NAME)
                            if IS_TRANSLATION_ENABLED:
                                url += f"/{lang}"
                            if original_url:
                                original_url = original_url.replace('twitter', DOMAIN_NAME)
                                if IS_TRANSLATION_ENABLED:
                                    original_url += f"/{lang}"

                        mention = f"{channel.guild.get_role(int(data['role_id'])).mention} " if data['role_id'] else ''
                        author, action = tweet.author.name, get_action(tweet)

                        tweet_text = build_delivery_text(tweet, current_p_tweet)
                        links = build_delivery_links(
                            url,
                            current_p_tweet,
                            tweet.is_quoted,
                            is_retweet=tweet.is_retweet,
                            original_url=original_url,
                        )
                        message_parts = []
                        if data['customized_msg']:
                            custom = re.sub(r":(\w+):", lambda match: replace_emoji(match, channel.guild), data['customized_msg']) if configs['emoji_auto_format'] else data['customized_msg']
                            message_parts.append(custom.format(mention=mention, author=author, action=action, url=url).rstrip())
                        elif mention:
                            message_parts.append(mention.rstrip())
                        if tweet_text:
                            message_parts.append(tweet_text)
                        if links:
                            message_parts.append(links)
                        msg = '\n'.join(part for part in message_parts if part)

                        media = await prepare_media_delivery(
                            self.session,
                            current_p_tweet if EMBED_TYPE == 'built_in' else None,
                            int(channel.guild.filesize_limit),
                        )
                        if media.fallback_urls:
                            msg = f'{msg}\n' + '\n'.join(media.fallback_urls)

                        webhook_name, avatar_url = build_webhook_identity(username, tweet, current_p_tweet)
                        await self.delivery.send(
                            channel,
                            content=msg,
                            username=webhook_name,
                            avatar_url=avatar_url,
                            embeds=None,
                            files=media.files,
                            view=current_view,
                            suppress_embeds=not media.fallback_urls,
                        )

                    except Exception as e:
                        await self.delivery_history.release(channel.id, claim_id)
                        log.error(f'an error occurred at {channel.mention} while sending notification: {e}')
                        continue

                    # The reservation already records claim_id. Also retain the
                    # wrapper/quote ID and referenced original for future checks.
                    # Keep the reservation if bookkeeping fails after a successful
                    # Discord send, otherwise a retry could create a duplicate.
                    try:
                        await self.delivery_history.record(channel.id, references.tweet_id, references.original_id)
                    except Exception as e:
                        log.error(f'failed to update delivery history for channel {channel.id}: {e}')

    async def tweetsUpdater(self, app: Twitter):
        updater_name = asyncio.current_task().get_name().split('_', 1)[1]
        while True:
            try:
                # Run the potentially blocking library call in a separate thread
                self.tweets[updater_name] = await asyncio.to_thread(app.get_tweet_notifications)
            except KeyError as e:
                # Handle the error thrown by `tweety-ns` mentioned in issue#59
                log.warning(f"handled KeyError in {updater_name}: {e}. This is likely a temporary API response issue from Twitter. Skipping this check.")
            except Exception as e:
                log.error(f'{e} (task : tweets updater {updater_name})')
                log.error(f"an unexpected error occurred, try again in {configs['tweets_updater_retry_delay']} minutes")
                await asyncio.sleep(configs['tweets_updater_retry_delay'] * 60)
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
