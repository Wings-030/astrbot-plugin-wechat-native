"""Trusted tool boundary and safe JSON business results."""

import json
from dataclasses import dataclass

from astrbot.api import logger

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
    def __init__(self, status):
        self.status = status


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
    if isinstance(error, ToolError):
        status = error.status
    elif isinstance(error, NativeAPIError):
        status = (
            "configuration_error"
            if error.status == 401 or error.code == "insufficient_scope"
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
    adapter: object
    account: str
    thread: str
    sender: str
    message_id: str


def context(event, *, write=False, group=False):
    from .adapter import WeChatNativeAdapter

    if not isinstance(event, WeChatNativeEvent) or not isinstance(
        event.adapter, WeChatNativeAdapter
    ):
        raise ToolError("not_available")
    adapter, raw = event.adapter, event.message_obj.raw_message
    cfg = adapter.native_config
    enabled = cfg.enable_wechat_actions if write else cfg.enable_history_tools
    if adapter.stopped or adapter._state_closed or adapter.client.closed or not enabled:
        raise ToolError("not_available")
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
        raise ToolError("not_available")
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
    return ToolContext(adapter, cfg.account_id, thread, sender, raw["id"])


def identity(item, ctx):
    if (
        not isinstance(item, dict)
        or item.get("accountId") != ctx.account
        or item.get("threadId") != ctx.thread
    ):
        raise ToolError("not_available")


def page_args(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ToolError("invalid_request")
    if not isinstance(cursor, str) or len(cursor) > 8192:
        raise ToolError("invalid_cursor")
