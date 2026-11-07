"""Durable, single-submission WeChat mutations. No confirmation state machine."""

import asyncio
import hashlib
import json
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
        client = self.adapter.admin_client
        if client is None:
            return result(row["action"], "configuration_error")
        await client.open()
        try:
            if operation is None:
                operation = (
                    await client.operation(row["operation_id"])
                    if row["operation_id"]
                    else await client.group_mutation_by_key(row["key"])
                )
            payload = operation.get("result")
            if (
                operation.get("accountId") != row["account"]
                or operation.get("kind") != "group_mutation"
                or not isinstance(operation.get("id"), str)
                or not operation["id"]
                or len(operation["id"]) > 512
                or (row["operation_id"] and row["operation_id"] != operation["id"])
                or not isinstance(payload, dict)
                or payload.get("action")
                != ("my_name" if row["action"] == "set_my_group_nickname" else "rename")
                or payload.get("targetId") != row["target"]
                or payload.get("groupId", row["thread"]) != row["thread"]
            ):
                raise ToolError("needs_review")
            state, remote = operation.get("state"), payload.get("remoteOutcome")
            if (
                state == "succeeded"
                and remote == "accepted"
                and payload.get("profileProjection") in {"complete", "partial", "pending"}
            ):
                status = "success"
            elif state in {"queued", "running"}:
                status = "pending"
            elif state == "failed" and remote in {"not_attempted", "rejected"}:
                status = (
                    "permission_denied" if payload.get("code") == "permission_denied" else "failed"
                )
            else:
                status = "needs_review"
            self.adapter.state.finish_action(row["key"], status, operation["id"])
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
        ctx = context(event, write=True, group=True)
        actions = ctx.adapter.actions
        actions.tasks.add(task)
        async with ctx.adapter.loop_guard.gate(ctx.thread):
            ctx = context(event, write=True, group=True)
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
