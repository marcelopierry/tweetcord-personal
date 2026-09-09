"""Verify X's subscription state, not merely the local notifier row."""
import asyncio


async def subscription_state(client, user_id):
    response = await asyncio.wait_for(
        client.request.get_friendship(client.me.id, str(user_id)), timeout=30,
    )
    relationship = response.get('relationship', {})
    source = relationship.get('source', {})
    target = relationship.get('target', {})
    if str(target.get('id_str', target.get('id'))) != str(user_id):
        raise RuntimeError('X returned an unexpected subscription target')
    if any(not isinstance(source.get(key), bool) for key in ('following', 'notifications_enabled')):
        raise RuntimeError('X omitted subscription state; cannot verify')
    return source


async def ensure_subscription(client, user_id):
    """Return whether repaired; never report success from an unchecked POST."""
    state = await subscription_state(client, user_id)
    if state['following'] and state['notifications_enabled']:
        return False
    if state.get('blocking') or state.get('blocked_by') or state.get('following_requested'):
        raise RuntimeError('X relationship prevents automatic subscription repair')
    if not state['following']:
        await asyncio.wait_for(client.follow_user(str(user_id)), timeout=30)
    await asyncio.wait_for(client.enable_user_notification(str(user_id)), timeout=30)
    state = await subscription_state(client, user_id)
    if not (state['following'] and state['notifications_enabled']):
        raise RuntimeError('X did not retain the requested subscription')
    return True
