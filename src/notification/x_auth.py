"""Reuse valid saved X cookies instead of repeatedly bootstrapping a login."""
import asyncio
from tweety import Twitter
from tweety.exceptions import InvalidCredentials
from src.notification.x_bootstrap import prepare_x_bootstrap
from src.log import setup_logger

log = setup_logger(__name__)


async def authenticate_x(name, token):
    app = prepare_x_bootstrap(Twitter(name))
    try:
        saved = app.session.cookies_dict()
        if app.session.logged_in and saved.get('auth_token') == token and saved.get('ct0'):
            try:
                if await asyncio.wait_for(app.connect(), 45):
                    log.info(f'reused verified saved X session for {name}')
                    return app
            except InvalidCredentials:
                pass  # A revoked session may require fresh cookies.
        await asyncio.wait_for(app.load_auth_token(token), 45)
        return app
    except BaseException:
        await app.request.session.aclose()
        raise


async def recover_x_account(name, token):
    """An X outage must never exit the Discord process or discard its queue."""
    delay = 60
    while True:
        try:
            return await authenticate_x(name, token)
        except Exception as error:
            # Exceptions can contain authenticated response bodies; log type only.
            log.error(f'X authentication unavailable for {name}: {type(error).__name__}; '
                      f'Discord stays connected; retrying X in {delay} seconds')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1800)
