import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from core.classes import Cog_Extension
from configs.load_configs import configs
from src.i18n import t
from src.permission import ADMINISTRATOR
from src.db_function.readonly_db import connect_readonly
from src.discord_ui.fetch_tracked_channels import fetch_tracked_channels
from src.discord_ui.pagination import Pagination

PSIZE = configs['users_list_pagination_size']
PCPOS = configs['users_list_page_counter_position']


class ListUsers(Cog_Extension):

    list_group = app_commands.Group(name='list', description=t('commands.list.description'), default_permissions=ADMINISTRATOR)

    async def _get_settings_rows(self, server_id: int, account: str = '', channel: str = '') -> list[tuple]:
        async with connect_readonly(os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')) as db:
            async with db.execute("""
                SELECT user.username, channel.id, notification.role_id, notification.enable_type, notification.enable_media_type, user.client_used
                FROM user
                JOIN notification
                ON user.id = notification.user_id
                JOIN channel
                ON notification.channel_id = channel.id
                WHERE channel.server_id = ? AND notification.enabled = 1
                AND (user.client_used = ? OR '' = ?)
                AND (channel.id = ? OR '' = ?)
                ORDER BY user.username COLLATE NOCASE, channel.id
            """, (str(server_id), account, account, channel, channel)) as cursor:
                return await cursor.fetchall()

    async def _get_account_usernames(self, server_id: int, channel_id: str = '') -> list[str]:
        async with connect_readonly(os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')) as db:
            async with db.execute("""
                SELECT DISTINCT user.username
                FROM user
                JOIN notification ON user.id = notification.user_id
                JOIN channel ON notification.channel_id = channel.id
                WHERE channel.server_id = ? AND notification.enabled = 1
                AND (channel.id = ? OR '' = ?)
                ORDER BY user.username COLLATE NOCASE
            """, (str(server_id), channel_id, channel_id)) as cursor:
                return [row[0] async for row in cursor]

    async def _send_settings(self, itn: discord.Interaction, account: str = '', channel: str = '') -> None:
        user_channel_role_data = await self._get_settings_rows(itn.guild_id, account, channel)

        media_labels = {
            '11': 'all posts',
            '10': 'text only',
            '01': 'media only',
        }
        formatted_data = [
            (
                f"{i + 1}. `@{username}` → <#{channel_id}>"
                f" · retweets: **{'on' if enable_type[0] == '1' else 'off'}**"
                f" · quote tweets: **{'on' if enable_type[1] == '1' else 'off'}**"
                f" · media: **{media_labels.get(enable_media_type, enable_media_type)}**"
                f" · role: {f'<@&{role_id}>' if role_id else 'none'}"
                f" · X login: `{client_used}`"
            )
            for i, (username, channel_id, role_id, enable_type, enable_media_type, client_used) in enumerate(user_channel_role_data)
        ]

        async def get_page(page: int):
            offset = (page - 1) * PSIZE
            page_data = formatted_data[offset:offset + PSIZE]
            total_pages = Pagination.compute_total_pages(len(formatted_data), PSIZE)
            page_counter = f' — page {page}/{total_pages}' if PCPOS == 'title' else ''
            description = 'No tracked accounts match this filter.' if not formatted_data else '\n'.join(page_data)
            embed = discord.Embed(
                title=f'TweetCord settings for {itn.guild.name}{page_counter}',
                description=description,
                color=0x778899,
            )
            if PCPOS == 'footer':
                embed.set_footer(text=f'Page {page}/{total_pages}')
            return embed, total_pages

        await Pagination(itn, get_page).navegate()

    @list_group.command(name='users', description=t('commands.list.users.description'))
    @app_commands.describe(
        account=t('commands.list.users.params.account'),
        channel=t('commands.list.users.params.channel'),
    )
    async def list_users(self, itn: discord.Interaction, account: str = '', channel: str = '') -> None:
        """Backward-compatible alias for the detailed settings list.

        Parameters:
        account: str, optional
            The client name that you want to filter.
        channel: str, optional
            The channel name that you want to filter.
        """

        await self._send_settings(itn, account, channel)

    @list_group.command(name='settings', description='Show each tracked X account with its notification settings.')
    @app_commands.describe(account='Optional X login used for tracking', channel='Optional destination channel')
    async def list_settings(self, itn: discord.Interaction, account: str = '', channel: str = '') -> None:
        """Show enabled notifier settings, including retweet and quote choices."""
        await self._send_settings(itn, account, channel)

    @list_group.command(name='accounts', description='Show tracked X accounts, optionally filtered by channel.')
    @app_commands.describe(channel='Optional channel to filter by; defaults to every channel')
    async def list_accounts(
        self,
        itn: discord.Interaction,
        channel: discord.TextChannel | discord.Thread = None,
    ) -> None:
        """Show a deduplicated list of tracked X accounts, optionally for one channel."""
        channel_id = str(channel.id) if channel else ''
        usernames = await self._get_account_usernames(itn.guild_id, channel_id)

        async def get_page(page: int):
            offset = (page - 1) * PSIZE
            page_usernames = usernames[offset:offset + PSIZE]
            total_pages = Pagination.compute_total_pages(len(usernames), PSIZE)
            page_counter = f' — page {page}/{total_pages}' if PCPOS == 'title' else ''
            empty_scope = channel.mention if channel else 'this server'
            description = f'No X accounts are currently tracked in {empty_scope}.' if not usernames else '\n'.join(
                f'{i + offset + 1}. `@{username}`' for i, username in enumerate(page_usernames)
            )
            scope = f' in #{channel.name}' if channel else f' in {itn.guild.name}'
            embed = discord.Embed(
                title=f'Tracked X accounts{scope}{page_counter}',
                description=description,
                color=0x778899,
            )
            if PCPOS == 'footer':
                embed.set_footer(text=f'Page {page}/{total_pages}')
            return embed, total_pages

        await Pagination(itn, get_page).navegate()

    @list_users.autocomplete('account')
    @list_settings.autocomplete('account')
    async def get_clients(self, itn: discord.Interaction, account: str) -> list[app_commands.Choice[str]]:
        async with connect_readonly(os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')) as db:
            db.row_factory = aiosqlite.Row
            async with db.cursor() as cursor:
                await cursor.execute('SELECT client_used FROM user WHERE enabled = 1')
                client_used = list(set([row['client_used'] async for row in cursor]))
                return [app_commands.Choice(name=row, value=row) for row in client_used if account.lower() in row.lower()]

    @list_users.autocomplete('channel')
    @list_settings.autocomplete('channel')
    async def get_channel(self, itn: discord.Interaction, input_channel: str) -> list[app_commands.Choice[str]]:
        return await fetch_tracked_channels(itn, input_channel, include_unknown=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ListUsers(bot))
