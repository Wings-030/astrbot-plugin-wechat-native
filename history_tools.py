"""One-page Native history, never an account-wide search or instruction source."""

import time
import uuid
from collections import OrderedDict

from .tool_support import ToolError, context, encode, error_result, identity, page_args, result


class HistoryTools:
    def __init__(self, adapter):
        self.adapter = adapter
        self.cursors = OrderedDict()

    async def group_info(self, ctx):
        data = await self.adapter.client.group(ctx.thread)
        if data.get("roomId") != ctx.thread or data.get("accountId", ctx.account) != ctx.account:
            raise ToolError("not_available")
        fields = (
            "displayName",
            "name",
            "ownerId",
            "myDisplayName",
            "announcement",
            "canMentionAll",
            "isInGroup",
        )
        safe = {}
        truncated = False
        for key in fields:
            value = data.get(key)
            if isinstance(value, str):
                truncated |= len(value) > 800
                safe[key] = value[:800]
            else:
                safe[key] = value if value is None or type(value) is bool else None
        admins = data.get("administratorIds")
        safe["administratorIds"] = None
        if isinstance(admins, list) and all(isinstance(x, str) for x in admins):
            safe["administratorIds"] = [x[:128] for x in admins[:50]]
            truncated |= len(admins) > 50 or any(len(x) > 128 for x in admins)
        return result(
            "get_group_info",
            "success",
            group=safe,
            truncated=truncated,
            coverage="saved_metadata_may_be_stale",
            dataOnly=True,
        )

    async def members(self, ctx, query, limit, cursor):
        page_args(limit, cursor)
        if not isinstance(query, str) or len(query.encode()) > 256:
            raise ToolError("invalid_request")
        query = query.strip()
        binding = (ctx.account, ctx.thread, "list_group_members", query, limit)
        native, page = self.cursor(cursor, binding)
        data = await self.adapter.client.group_members(ctx.thread, limit=limit, cursor=native)
        if not isinstance(data.get("items"), list) or len(data["items"]) > limit:
            raise ToolError("not_available")
        safe = []
        for item in data["items"]:
            if not isinstance(item, dict):
                raise ToolError("not_available")
            if item.get("deleted") is True:
                continue
            if (
                item.get("accountId", ctx.account) != ctx.account
                or item.get("groupId", ctx.thread) != ctx.thread
                or item.get("threadId", ctx.thread) != ctx.thread
            ):
                raise ToolError("not_available")
            member = {
                key: item[key][:800] if isinstance(item.get(key), str) else None
                for key in ("id", "username", "displayName", "roomDisplayName", "mentionName")
            }
            if not member["id"] or not member["username"]:
                raise ToolError("not_available")
            if not query or any(
                query.casefold() in (value or "").casefold() for value in member.values()
            ):
                safe.append(member)
        next_cursor = self.next_cursor(data.get("nextCursor"), binding, page)
        return result(
            "list_group_members",
            "success",
            items=safe,
            count=len(safe),
            nextCursor=next_cursor,
            hasMore=data.get("nextCursor") is not None,
            truncated=bool(data.get("nextCursor") and not next_cursor),
            coverage="current_page_filter_may_be_partial"
            if query
            else "saved_members_may_be_partial",
            dataOnly=True,
        )

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

    async def recent(self, ctx, limit, cursor, query=None, *, action="recent_messages"):
        page_args(limit, cursor)
        if action == "search_messages":
            if not isinstance(query, str) or not query.strip() or len(query.strip().encode()) > 256:
                raise ToolError("invalid_request")
            query = query.strip()
        binding = (ctx.account, ctx.thread, action, query or "", limit)
        native_cursor, page = self.cursor(cursor, binding)
        data = await self.adapter.client.messages(
            ctx.thread, limit=limit, cursor=native_cursor, query=query or ""
        )
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
            action,
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
            **({"query": query} if query is not None else {}),
        )


async def run_read(event, action, **params):
    try:
        ctx = context(event, group=action in {"get_group_info", "list_group_members"})
        if action == "get_group_info":
            return encode(await ctx.adapter.history.group_info(ctx))
        if action == "list_group_members":
            return encode(await ctx.adapter.history.members(ctx, **params))
        return encode(await ctx.adapter.history.recent(ctx, action=action, **params))
    except Exception as error:
        return encode(error_result(action, error))
