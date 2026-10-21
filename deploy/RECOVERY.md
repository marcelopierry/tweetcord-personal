# Outage and connection-storm protection

The October 21 incident started when an automatic Ubuntu maintenance restart
forced a fresh X login. Tweety's token bootstrap contacted business.x.com,
which returned a Cloudflare challenge despite a valid saved X session. The
tracker called sys.exit(1), and Restart=always/RestartSec=10 repeatedly opened
new Discord sessions until Discord reset the bot token.

The bot now reuses matching saved X cookies, verifies them, and retries X
authentication with 60-second to 30-minute backoff inside the same process.
An X outage never intentionally exits Discord. Failed clients are closed and
exceptions are logged by type without authenticated response bodies.

Startup runs once in setup_hook. Repeated Discord READY events update presence
without reloading cogs, restarting trackers, resetting timestamps or resyncing
commands. Saved account timestamps are preserved by default on restart.

Every fresh Discord IDENTIFY reserves a persistent slot in
DATA_PATH/discord-session-starts.db: at most 20 starts per rolling 24 hours,
at least 60 seconds apart. Reaching this budget waits in-process; ordinary
session RESUME operations do not consume this budget. Do not remove the
database to bypass the safeguard.

Install tweetcord-safety.conf as
/etc/systemd/system/tweetcord.service.d/safety.conf, then run systemctl
daemon-reload. This limits process starts to four per hour, waits five minutes
between crash restarts, and never restarts exit code 78 (invalid credentials,
privileged intent configuration, or other startup configuration failures).
After correcting a blocked service, use systemctl reset-failed tweetcord and
systemctl start tweetcord. Do not use Restart=always with a short delay.

A revoked Discord token must be regenerated in the Discord Developer Portal and
installed privately in the server .env. These safeguards prevent the identified
connection storm; they cannot prevent independent provider outages or revocation.
