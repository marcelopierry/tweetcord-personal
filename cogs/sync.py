import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from core.classes import Cog_Extension
from src.i18n import t
from src.log import setup_logger
from src.sync_db.sync_db import sync_db
from src.db_function.readonly_db import connect_readonly

log = setup_logger(__name__)


class Sync(Cog_Extension):

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name='sync', description=t('commands.sync.description'))
    async def sync(self, itn: discord.Interaction):
        """To sync the notification of new Twitter account with database, use this command."""

        await itn.response.defer(ephemeral=True)

        async with connect_readonly(os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT id, client_used FROM user WHERE enabled = 1') as cursor:
                follow_list = {row[0]: row[1] async for row in cursor}

        notification = self.bot.get_cog('Notification')
        clients = notification.account_tracker.x_clients if notification else {}
        missing = set(follow_list.values()) - set(clients)
        if missing:
            await itn.followup.send(
                'X account(s) still reconnecting: ' + ', '.join(sorted(missing)) +
                '. Please try again shortly.', ephemeral=True,
            )
            return
        self.bot.loop.create_task(sync_db(follow_list, clients))

        await itn.followup.send(t('sync.background'), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Sync(bot))
