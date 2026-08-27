/opt/homebrew/Library/Homebrew/cmd/shellenv.sh: line 18: /bin/ps: Operation not permitted
# Personal Tweetcord

A private, self-hosted Discord bot for forwarding posts from selected X/Twitter accounts into selected Discord channels. This is based on [TweetCord](https://github.com/Yuuzi261/Tweetcord) and remains MIT-licensed.

## What is customized

- Five-minute delivery delay by default (`notification_delay_seconds: 300`). A queued post is refreshed if a later poll sees a changed version before delivery.
- Lower polling frequency by default (`tweets_check_period: 60`) to reduce scraping pressure for a personal installation.
- Per-notifier controls for original posts, retweets, and quote posts remain available through `/add notifier` and `/customize settings`.
- TweetCord's existing embed pipeline handles photos, multiple photos, video/GIF previews, quote-tweet text, and retweet text. Set `embed.type: built_in` for rich media embeds.
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

Then invite the bot with `View Channel`, `Send Messages`, `Embed Links`, `Attach Files`, `Mention Everyone`, and `Use Slash Commands`. Use `/add notifier` to choose the X account, Discord channel, media filter, and whether retweets/quotes are enabled for that notifier.

## Important operating cost note

The software itself has no paid service requirement. Hosting is $0 if it runs on your computer, NAS, or another machine you already own. A free cloud VM is possible but depends on the provider's current terms and availability; do not assume a permanent free tier.
