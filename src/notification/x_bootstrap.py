"""Keep authenticated X sessions authenticated during request initialization."""
from types import MethodType
from tweety.transaction import TransactionGenerator


def prepare_x_bootstrap(app):
    request = app.request
    original = request._init_local_api

    async def initialize(self):
        if not self.cookies.get('auth_token'):
            return await original()
        # X's new logged-out app no longer contains the transaction bootstrap
        # assets. The authenticated home page still does. Do not strip the
        # user's existing cookies before requesting that page, as Tweety does.
        if not self._transaction:
            page = await self.get_home_html()
            self._transaction = TransactionGenerator(page)
        if not self._guest_token:
            self._guest_token = await self._get_guest_token()

    request._init_local_api = MethodType(initialize, request)
    return app
