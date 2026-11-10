"""Trusted tool boundary and safe JSON business results."""

import json
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import Platform

from .event import WeChatNativeEvent, session_id, session_target
from .native_client import NativeAPIError

MESSAGES = {
    "success": "操作已完成",
    "permission_denied": "当前没有权限执行这个操作",
    "not_available": "当前暂时无法使用这个功能",
    "configuration_error": "当前功能的权限配置尚未准备好",
    "invalid_request": "请求参数无效，请调整后再试",
    "not_in_group_chat": "这个功能要在群里使用",
    "invalid_cursor": "分页信息已失效，请重新查询",
    "ambiguous_target": "请明确要撤回哪一条消息",
    "already_recalled": "这条消息已经撤回",
    "not_recallable": "这条消息目前无法撤回",
    "rate_limited": "操作太频繁，请稍后再试",
    "pending": "操作已受理，仍在处理中",
    "needs_review": "操作结果暂时无法确认，请勿重复提交",
    "failed": "操作未能完成",
}


class ToolError(Exception):
    def __init__(self, status, reason="business_state"):
        self.status = status
        self.reason = reason


def result(action, status, **fields):
    return {
        "ok": status == "success",
        "status": status,
        "action": action,
        "message": MESSAGES[status],
        "retryable": False,
        **fields,
    }


def encode(value):
    while True:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= 16384:
            return text
        if value.get("items"):
            value["items"].pop()
            value["count"] = len(value["items"])
            value["truncated"] = True
        else:
            return json.dumps(result(value["action"], "not_available"), ensure_ascii=False)


def error_result(action, error):
    error_type = type(error)
    # importlib.reload replaces ToolError, but existing action instances retain
    # its old class. Trust only classes defined in this module's namespace;
    # matching an exception name or status attribute alone is insufficient.
    local_tool_error = (
        error_type.__module__ == ToolError.__module__
        and error_type.__qualname__ == ToolError.__qualname__
        and getattr(error_type.__init__, "__globals__", None) is globals()
        and getattr(error, "status", None) in MESSAGES
    )
    if isinstance(error, ToolError) or local_tool_error:
        status = error.status
        logger.warning(
            "WeChat Native 工具拒绝：action=%s status=%s reason=%s", action, status, error.reason
        )
    elif isinstance(error, NativeAPIError):
        logger.warning(
            "WeChat Native 工具请求失败：action=%s http=%s code=%s",
            action,
            error.status,
            error.code,
        )
        status = (
            "invalid_cursor"
            if error.code == "invalid_cursor"
            else "configuration_error"
            if error.status == 401 or error.code in {"insufficient_scope", "forbidden"}
            else "permission_denied"
            if error.status == 403
            else "rate_limited"
            if error.status == 429
            else "not_available"
        )
    else:
        # No exception repr/trace: upstream errors can embed secrets or chat text.
        logger.warning("WeChat Native 工具失败：action=%s type=%s", action, type(error).__name__)
        status = "failed"
    return result(action, status)


@dataclass(frozen=True)
class ToolContext:
    event: object
    adapter: object
    account: str
    thread: str
    sender: str
    message_id: str


def _is_owned_reloaded_native_event(event, adapter, adapter_type):
    """Allow class identity drift only for a trusted event from an adapter generation."""
    if not isinstance(event, AstrMessageEvent) or not isinstance(adapter, Platform):
        return False

    event_type, actual_adapter_type = type(event), type(adapter)
    if (event_type.__module__, event_type.__qualname__) != (
        WeChatNativeEvent.__module__,
        WeChatNativeEvent.__qualname__,
    ) or (actual_adapter_type.__module__, actual_adapter_type.__qualname__) != (
        adapter_type.__module__,
        adapter_type.__qualname__,
    ):
        return False

    # Resolve the classes held by the adapter's own module generation.
    # A matching name alone must never authorize a tool invocation.
    factory = getattr(actual_adapter_type, "create_event", None)
    namespace = getattr(factory, "__globals__", None)
    if not isinstance(namespace, dict) or (
        namespace.get("WeChatNativeEvent") is not event_type
        or namespace.get("WeChatNativeAdapter") is not actual_adapter_type
    ):
        return False

    message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    retained = getattr(adapter, "_events", None)
    return (
        isinstance(message_id, str)
        and bool(message_id)
        and retained is not None
        and retained.get(message_id) is event
    )


def context(event, *, write=False, group=False, send=False):
    from .adapter import WeChatNativeAdapter

    adapter = getattr(event, "adapter", None)
    event_match = isinstance(event, WeChatNativeEvent)
    adapter_match = isinstance(adapter, WeChatNativeAdapter)
    if not (event_match and adapter_match) and not _is_owned_reloaded_native_event(
        event, adapter, WeChatNativeAdapter
    ):
        if not event_match:
            logger.warning(
                "WeChat Native 工具事件类型：actual=%s.%s expected=%s.%s",
                type(event).__module__,
                type(event).__qualname__,
                WeChatNativeEvent.__module__,
                WeChatNativeEvent.__qualname__,
            )
            raise ToolError("not_available", "event_type_mismatch")
        raise ToolError("not_available", "adapter_type_mismatch")
    raw = event.message_obj.raw_message
    cfg = adapter.native_config
    enabled = cfg.enable_wechat_actions if write else cfg.enable_history_tools
    if adapter.stopped or adapter._state_closed:
        raise ToolError("not_available", "adapter_stopped")
    if adapter.client.closed:
        raise ToolError("not_available", "native_client_closed")
    if not send and not enabled:
        raise ToolError(
            "not_available", "wechat_actions_disabled" if write else "history_tools_disabled"
        )
    if not isinstance(raw, dict) or any(
        not isinstance(raw.get(k), str) or not raw[k]
        for k in ("id", "accountId", "threadId", "senderId")
    ):
        raise ToolError("invalid_request")
    thread, sender = raw["threadId"], raw["senderId"]
    expected_session = session_id(cfg.account_id, thread, sender if cfg.unique_session else None)
    try:
        valid = (
            raw["accountId"] == cfg.account_id
            and raw["id"] == event.message_obj.message_id
            and raw.get("direction") == "incoming"
            and sender == event.get_sender_id()
            and sender != adapter.bot_self_id
            and event.message_obj.self_id == adapter.bot_self_id
            and event.session.platform_id == adapter.meta().id
            and event.session.session_id == expected_session
            and session_target(event.session.session_id, cfg.account_id) == thread
            and adapter.state.message_status(raw["id"]) in {"queued", "replied"}
            and adapter.state.message_matches(raw["id"], thread, raw.get("serverId"))
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or raw.get("recall") is not None or event.is_stopped():
        raise ToolError("permission_denied")
    if adapter.loop_guard.blocked(thread):
        raise ToolError("not_available", "loop_cooldown")
    is_group = thread.endswith("@chatroom")
    if group and not is_group:
        raise ToolError("not_in_group_chat")
    if is_group:
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            raise ToolError("permission_denied")
        mentions = raw.get("mentions")
        if cfg.group_reply_mode == "mention" and not (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        ):
            raise ToolError("permission_denied")
    elif not cfg.enable_private_reply or (cfg.allowed_users and sender not in cfg.allowed_users):
        raise ToolError("permission_denied")
    return ToolContext(event, adapter, cfg.account_id, thread, sender, raw["id"])


def identity(item, ctx):
    if (
        not isinstance(item, dict)
        or item.get("accountId") != ctx.account
        or item.get("threadId") != ctx.thread
    ):
        raise ToolError("not_available", "message_scope_mismatch")


def page_args(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ToolError("invalid_request")
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise ToolError("invalid_cursor")
