import asyncio

from tweety.exceptions import TwitterError

from src.log import setup_logger
from src.utils import get_accounts
from src.notification.x_auth import authenticate_x

log = setup_logger(__name__)


async def sync_db(follow_list: dict[str, str], clients=None) -> None:

    apps = clients if clients is not None else {}
    owned = []
    try:
        if clients is None:
            # Retain standalone usage, with the same protected authentication.
            for account_name, token in get_accounts().items():
                app = await authenticate_x(account_name, token)
                apps[account_name] = app
                owned.append(app)

        for user_id, client_used in follow_list.items():
            app = apps.get(client_used)
            if app is None:
                log.warning(f'client "{client_used}" for user {user_id} is not configured, skip synchronization')
                continue

            try:
                await app.follow_user(user_id)
                await app.enable_user_notification(user_id)
            except TwitterError as e:
                if '[108]' in str(e):
                    log.warning(f'cannot find specified user: {user_id}, skip synchronization')
                else:
                    log.error(f'failed to sync {user_id} to {client_used}: {type(e).__name__}')
            except Exception as e:
                log.error(f'failed to sync {user_id} to {client_used}: {type(e).__name__}')

            await asyncio.sleep(1)
    finally:
        for app in owned:
            await app.request.session.aclose()

    log.info('synchronization with database completed')
