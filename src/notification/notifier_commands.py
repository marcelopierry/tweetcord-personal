from typing import Any


def notifier_settings_unchanged(notification: Any, role_id: str, enable_type: str, media_type: str) -> bool:
    """Return whether an enabled notifier already has the requested settings."""
    if notification is None:
        return False
    return (
        int(notification['enabled']) == 1
        and str(notification['role_id'] or '') == str(role_id or '')
        and str(notification['enable_type']) == str(enable_type)
        and str(notification['enable_media_type']) == str(media_type)
    )


def selected_interaction_channel_id(interaction: Any, explicit_channel_id: str | None = None) -> str | None:
    """Resolve an optional slash-command channel, defaulting to its current channel."""
    if explicit_channel_id:
        return str(explicit_channel_id)

    for group in (getattr(interaction, 'data', None) or {}).get('options', []):
        for option in group.get('options', []):
            if option.get('name') in {'channel', 'channel_id'} and option.get('value'):
                return str(option['value'])

    channel_id = getattr(interaction, 'channel_id', None)
    return str(channel_id) if channel_id is not None else None
