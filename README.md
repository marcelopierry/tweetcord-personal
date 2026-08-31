# Personal Tweetcord

A private, self-hosted Discord bot for forwarding posts from selected X/Twitter accounts into selected Discord channels. This is based on [TweetCord](https://github.com/Yuuzi261/Tweetcord) and remains MIT-licensed.

## What is customized

- Three-minute delivery delay by default (`notification_delay_seconds: 180`). A queued post is refreshed if a later poll sees a changed version before delivery.
- `/customize send-delay` changes that post-detection delay persistently and immediately reschedules every queued post.
- Lower polling frequency by default (`tweets_check_period: 60`) to reduce scraping pressure for a personal installation.
- Per-notifier controls for original posts, retweets, and quote posts remain available through `/add notifier` and `/customize settings`.
- Tweet text is displayed in a compact Discord embed card while links and uploaded media remain separate. Quote tweets are ordered as the quote-post link, `🔁` plus the original link, the quote author's card, the original author's card, and finally any photos/videos. Retweets use `🔄` and are suppressed per channel when that original has already been delivered; quote tweets are never suppressed merely because their original was seen.
- Deliveries use a channel webhook when the bot has **Manage Webhooks**, so each post can display the tracked X account's name and avatar without changing the bot's global identity. The bot falls back to its normal identity if that permission is unavailable.
- With `embed.type: built_in`, photos and videos are downloaded and uploaded as Discord attachments when they fit the channel upload limit. Uploaded videos play in Discord; oversized media falls back to a direct media URL instead of requiring the X post page.
- External links found in tweet text are sent as individual follow-up links so Discord can render native article, website, and video previews below the tweet card. X profile, hashtag, and status links are excluded to avoid duplicate previews.
- Startup defaults to ignoring the backlog from before the bot came online (`init_latest_tweet_on_startup: true`).

The delay reduces duplicate noise from quick changes, but no unauthenticated X scraper can guarantee that a deleted post is never seen or that every edit is observable.

## Run locally for $0/month

1. Install Python 3.11+ and Docker (optional).
2. Copy `.env.example` to `.env` and fill in a Discord bot token and one or more X auth tokens. Keep `.env` private.
3. Copy `configs.example.yml` to `configs.yml`.
4. Install dependencies and run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

For a machine that is always on, Docker Compose is also available:

```bash
docker compose up -d
```

Then invite the bot with `View Channel`, `Send Messages`, `Embed Links`, `Attach Files`, `Manage Webhooks`, `Mention Everyone`, and `Use Slash Commands`. Use `/add notifier` to choose the X account, media filter, and whether retweets/quotes are enabled for that notifier. The channel is optional and defaults to the channel where the command is used.

## Important operating cost note

The software itself has no paid service requirement. Hosting is $0 if it runs on your computer, NAS, or another machine you already own. A free cloud VM is possible but depends on the provider's current terms and availability; do not assume a permanent free tier.
