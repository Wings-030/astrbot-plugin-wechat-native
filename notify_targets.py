"""Discover notification targets through AstrBot's public session metadata APIs."""

from datetime import UTC

NOTIFY_TYPE = "weixin_oc"


def active_platforms(context, name: str) -> list:
    manager = getattr(context, "platform_manager", None)
    instances = getattr(manager, "platform_insts", None) or []
    result = []
    for instance in instances:
        if getattr(instance, "stopped", False):
            continue
        try:
            meta = instance.meta()
        except Exception:
            continue
        if meta.name == name:
            result.append(instance)
    return result


def account_is_online(account: dict) -> bool:
    return bool(account.get("authenticated") and account.get("connection") == "online")


async def best_weixin_umo(context) -> str | None:
    platform_ids = {instance.meta().id for instance in active_platforms(context, NOTIFY_TYPE)}
    if not platform_ids:
        return None
    database = context.get_db()
    aliases = await database.get_umo_aliases()
    candidates = {
        alias.umo: 0.0 for alias in aliases if is_weixin_private_umo(alias.umo, platform_ids)
    }
    conversations, _ = await database.get_filtered_conversations(
        page=1,
        page_size=2,
        platform_ids=sorted(platform_ids),
        message_types=["FriendMessage"],
        include_history=False,
        group_by_session=True,
        sort_by="updated_at",
        sort_order="desc",
    )
    for conversation in conversations:
        umo = conversation.user_id
        if not is_weixin_private_umo(umo, platform_ids):
            continue
        updated = conversation.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        candidates[umo] = max(candidates.get(umo, 0.0), updated.timestamp())
    if not candidates:
        return None
    ranked = sorted(candidates, key=candidates.get, reverse=True)
    if len(ranked) > 1 and candidates[ranked[0]] == candidates[ranked[1]]:
        return None
    return ranked[0]


def is_weixin_private_umo(umo: str, platform_ids: set[str]) -> bool:
    parts = umo.split(":", 2) if isinstance(umo, str) else []
    return (
        len(parts) == 3
        and parts[0] in platform_ids
        and parts[1] == "FriendMessage"
        and bool(parts[2])
    )
