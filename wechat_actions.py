"""Durable, single-submission WeChat mutations. No confirmation state machine."""

import asyncio
import hashlib
import json
import re
import time
import unicodedata

from .native_client import NativeAPIError
from .tool_support import ToolError, context, encode, error_result, result

FINAL = {
    "success",
    "permission_denied",
    "configuration_error",
    "failed",
    "not_available",
    "already_recalled",
    "not_recallable",
}


class WeChatActions:
    def __init__(self, adapter):
        self.adapter = adapter
        self.tasks = set()

    async def inspect(self, row, operation=None):
        if row["own_sender"] != self.adapter.bot_self_id:
            self.adapter.state.finish_action(row["key"], "needs_review")
            return result(row["action"], "needs_review")
        if row["status"] in FINAL:
            return result(row["action"], row["status"])
        recall = row["action"] == "recall_own_message"
        client = self.adapter.client if recall else self.adapter.admin_client
        if client is None:
            return result(row["action"], "configuration_error")
        await client.open()
        try:
            if operation is None:
                # v1.5.1 exposes no read-by-key endpoint for recalls. A lost
                # admission response without an operation ID stays uncertain.
                if recall and not row["operation_id"]:
                    return result(row["action"], "needs_review")
                operation = (
                    await client.operation(row["operation_id"])
                    if row["operation_id"]
                    else await client.group_mutation_by_key(row["key"])
                )
            payload = operation.get("result")
            if (
                operation.get("accountId") != row["account"]
                or operation.get("kind") != ("message_recall" if recall else "group_mutation")
                or not isinstance(operation.get("id"), str)
                or not operation["id"]
                or len(operation["id"]) > 512
                or (row["operation_id"] and row["operation_id"] != operation["id"])
                or not isinstance(payload, dict)
            ):
                raise ToolError("needs_review")
            if recall:
                if (
                    payload.get("messageId") != row["target"]
                    or payload.get("threadId") != row["thread"]
                    or payload.get("serverId") != row["server_id"]
                ):
                    raise ToolError("needs_review")
            elif (
                payload.get("action")
                != ("my_name" if row["action"] == "set_my_group_nickname" else "rename")
                or payload.get("targetId") != row["target"]
                or payload.get("groupId", row["thread"]) != row["thread"]
            ):
                raise ToolError("needs_review")
            state, remote = operation.get("state"), payload.get("remoteOutcome")
            if (
                state == "succeeded"
                and remote in ({"accepted", "confirmed"} if recall else {"accepted"})
                and payload.get("profileProjection") in {"complete", "partial", "pending"}
                and (not recall or payload.get("status") in {"recalled", "already_recalled"})
            ):
                status = (
                    "already_recalled"
                    if recall and payload.get("status") == "already_recalled"
                    else "success"
                )
            elif state in {"queued", "running"}:
                status = "pending"
            elif state == "failed" and remote in {"not_attempted", "rejected"}:
                status = (
                    "not_recallable"
                    if recall
                    and payload.get("status") in {"not_self", "unavailable", "not_eligible"}
                    else (
                        "permission_denied"
                        if payload.get("code") == "permission_denied"
                        else "failed"
                    )
                )
            else:
                status = "needs_review"
            self.adapter.state.finish_action(row["key"], status, operation["id"])
            if recall and status in {"success", "already_recalled"}:
                self.adapter.state.recall(row["thread"], row["server_id"], row["target"])
            return result(row["action"], status)
        except Exception:
            self.adapter.state.finish_action(row["key"], "needs_review")
            return result(row["action"], "needs_review")

    async def recover(self):
        for row in self.adapter.state.pending_actions(self.adapter.native_config.account_id):
            if self.adapter.stopped:
                return
            async with self.adapter.loop_guard.gate(row["thread"]):
                await self.inspect(row)

    async def recall(self, ctx, message_id):
        action, store = "recall_own_message", self.adapter.state
        if ctx.sender not in self.adapter.native_config.wechat_action_admins:
            raise ToolError("permission_denied")
        existing = store.action_intent(ctx.account, ctx.thread, ctx.message_id)
        if existing:
            if existing["action"] != action:
                raise ToolError("invalid_request")
            return await self.inspect(existing)
        if (
            not isinstance(message_id, str)
            or len(message_id) > 512
            or any(unicodedata.category(c) == "Cc" for c in message_id)
        ):
            raise ToolError("invalid_request")
        if not message_id:
            page = await self.adapter.client.messages(ctx.thread, limit=20)
            if not isinstance(page.get("items"), list) or len(page["items"]) > 20:
                raise ToolError("not_available")
            candidates = []
            for item in page["items"]:
                if not isinstance(item, dict):
                    raise ToolError("not_available")
                if item.get("deleted") is True:
                    continue
                if item.get("accountId") != ctx.account or item.get("threadId") != ctx.thread:
                    raise ToolError("permission_denied")
                if item.get("direction") == "outgoing" and item.get("senderId") in (
                    None,
                    self.adapter.bot_self_id,
                ):
                    candidates.append(item)
            if len(candidates) != 1:
                raise ToolError("ambiguous_target")
            message_id = candidates[0].get("id")
            if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
                raise ToolError("ambiguous_target")
            selected_server = candidates[0].get("serverId")
        else:
            selected_server = None
        target = await self.adapter.client.message(message_id)
        if (
            target.get("id") != message_id
            or target.get("accountId") != ctx.account
            or target.get("threadId") != ctx.thread
        ):
            raise ToolError("permission_denied")
        if selected_server is not None and target.get("serverId") != selected_server:
            raise ToolError("needs_review")
        if target.get("direction") != "outgoing" or target.get("senderId") not in (
            None,
            self.adapter.bot_self_id,
        ):
            raise ToolError("not_recallable")
        if target.get("recall") is not None or store.message_status(message_id) == "recalled":
            return result(action, "already_recalled")
        server_id = target.get("serverId")
        if (
            not isinstance(server_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", server_id)
            or int(server_id) > 18446744073709551615
        ):
            raise ToolError("not_recallable")
        if not store.recall_target_matches(ctx.account, message_id, ctx.thread, server_id):
            raise ToolError("needs_review")
        context(ctx.event, write=True)
        param_hash = hashlib.sha256(server_id.encode()).hexdigest()
        key = (
            "astrbot-action-"
            + hashlib.sha256(
                json.dumps(
                    [ctx.account, ctx.thread, ctx.message_id, action, message_id, param_hash]
                ).encode()
            ).hexdigest()
        )
        fresh = store.claim_action(
            ctx.account,
            ctx.thread,
            ctx.message_id,
            action,
            key,
            param_hash,
            message_id,
            server_id,
            self.adapter.bot_self_id,
            time.time(),
        )
        row = store.action_intent(ctx.account, ctx.thread, ctx.message_id)
        if not fresh:
            return await self.inspect(row)
        try:
            operation = await self.adapter.client.recall_message(message_id, server_id, key)
        except NativeAPIError as error:
            if error.code in {"recall_identity_mismatch", "recall_target_changed"}:
                status = "needs_review"
            elif error.code == "message_recalled":
                status = "already_recalled"
            elif error.code == "recall_target_unavailable":
                status = "not_recallable"
            elif 400 <= error.status < 500 and error.status not in {408, 429}:
                status = error_result(action, error)["status"]
            else:
                status = "needs_review"
            store.finish_action(key, status)
            return result(action, status)
        return await self.inspect(row, operation)

    async def change_group(self, ctx, action, name):
        if (
            action == "rename_group"
            and ctx.sender not in self.adapter.native_config.wechat_action_admins
        ):
            raise ToolError("permission_denied")
        store = self.adapter.state
        existing = store.action_intent(ctx.account, ctx.thread, ctx.message_id)
        if existing:
            if existing["action"] != action:
                raise ToolError("invalid_request")
            return await self.inspect(existing)
        if not isinstance(name, str):
            raise ToolError("invalid_request")
        name = name.strip()
        if (
            not name
            or len(name.encode()) > 512
            or any(unicodedata.category(c) == "Cc" for c in name)
        ):
            raise ToolError("invalid_request")
        previous = store.recent_action(ctx.account, ctx.thread, action)
        if previous and previous["status"] in {"pending", "needs_review"}:
            return await self.inspect(previous)
        if (
            action == "set_my_group_nickname"
            and previous
            and time.time() - previous["created"] < 60
        ):
            raise ToolError("rate_limited")
        client = self.adapter.admin_client
        if client is None:
            raise ToolError("configuration_error")
        await client.open()
        context(ctx.event, write=True, group=True)
        param_hash = hashlib.sha256(name.encode()).hexdigest()
        key = (
            "astrbot-action-"
            + hashlib.sha256(
                json.dumps(
                    [ctx.account, ctx.thread, ctx.message_id, action, ctx.thread, param_hash]
                ).encode()
            ).hexdigest()
        )
        fresh = store.claim_action(
            ctx.account,
            ctx.thread,
            ctx.message_id,
            action,
            key,
            param_hash,
            ctx.thread,
            None,
            self.adapter.bot_self_id,
            time.time(),
        )
        row = store.action_intent(ctx.account, ctx.thread, ctx.message_id)
        if not fresh:
            return await self.inspect(row)
        try:
            mutation = (
                client.set_my_group_name
                if action == "set_my_group_nickname"
                else client.rename_group
            )
            operation = await mutation(ctx.thread, name, key)
        except NativeAPIError as error:
            if 400 <= error.status < 500 and error.status not in {408, 429}:
                value = error_result(action, error)
                store.finish_action(key, value["status"])
                return value
            return await self.inspect(row)
        # Cancellation or any unexpected failure leaves the already committed intent uncertain.
        return await self.inspect(row, operation)


async def run_action(event, action, **params):
    task, actions = asyncio.current_task(), None
    try:
        ctx = context(event, write=True, group=action != "recall_own_message")
        actions = ctx.adapter.actions
        actions.tasks.add(task)
        async with ctx.adapter.loop_guard.gate(ctx.thread):
            ctx = context(event, write=True, group=action != "recall_own_message")
            if action == "recall_own_message":
                return encode(await actions.recall(ctx, **params))
            return encode(
                await actions.change_group(
                    ctx,
                    action,
                    params["nickname"] if action == "set_my_group_nickname" else params["name"],
                )
            )
    except Exception as error:
        return encode(error_result(action, error))
    finally:
        if actions:
            actions.tasks.discard(task)
