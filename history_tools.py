"""One-page Native history, never an account-wide search or instruction source."""

import time
import uuid
from collections import OrderedDict

from .tool_support import ToolError, context, encode, error_result, identity, page_args, result


class HistoryTools:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cursors = OrderedDict()

    def cursor(self, value, binding):
        if not value:
            return "", 0
        stored = self.cursors.get(value)
        if not stored or stored[0] != binding or stored[3] < time.monotonic():
            raise ToolError("invalid_cursor")
        return stored[1], stored[2]

    def next_cursor(self, native, binding, page):
        if native is None:
            return None
        if not isinstance(native, str) or not native or len(native) > 8192:
            raise ToolError("invalid_cursor")
        if page >= 20:
            return None
        token = uuid.uuid4().hex
        self.cursors[token] = (binding, native, page + 1, time.monotonic() + 600)
        while len(self.cursors) > 512:
            self.cursors.popitem(last=False)
        return token

    async def recent(self, ctx, limit, cursor):
        page_args(limit, cursor)
        binding = (ctx.account, ctx.thread, "recent_messages", "", limit)
        native_cursor, page = self.cursor(cursor, binding)
        data = await self.adapter.client.messages(ctx.thread, limit=limit, cursor=native_cursor)
        items = data.get("items")
        if not isinstance(items, list) or len(items) > limit:
            raise ToolError("not_available")
        safe = []
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not 1 <= len(item["id"]) <= 512
            ):
                raise ToolError("not_available")
            if item.get("deleted") is True:
                safe.append({"messageId": item["id"], "text": "[已删除]"})
                continue
            identity(item, ctx)
            body = item.get("body") if isinstance(item.get("body"), str) else ""
            text = (
                "[已撤回]"
                if item.get("recall") is not None
                else (body if item.get("type") in {1, 49} else "[媒体消息]")
            )
            safe.append(
                {
                    "messageId": item["id"],
                    "senderId": item["senderId"][:256]
                    if isinstance(item.get("senderId"), str)
                    else None,
                    "createdAt": item["createdAt"][:64]
                    if isinstance(item.get("createdAt"), str)
                    else None,
                    "direction": item.get("direction")
                    if item.get("direction") in ("incoming", "outgoing")
                    else None,
                    "type": item.get("type") if type(item.get("type")) is int else None,
                    "text": text[:800],
                    "truncated": len(text) > 800,
                }
            )
        next_cursor = self.next_cursor(data.get("nextCursor"), binding, page)
        return result(
            "recent_messages",
            "success",
            scope="current_chat",
            source="native_local_history",
            items=safe,
            count=len(safe),
            nextCursor=next_cursor,
            hasMore=data.get("nextCursor") is not None,
            truncated=bool(data.get("nextCursor") and not next_cursor),
            coverage="local_history_may_be_partial",
            dataOnly=True,
        )


async def run_read(event, action, **params):
    try:
        ctx = context(event)
        return encode(await ctx.adapter.history.recent(ctx, **params))
    except Exception as error:
        return encode(error_result(action, error))
