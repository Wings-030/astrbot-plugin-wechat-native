import asyncio
import json
from dataclasses import replace

import pytest
from conftest import module
from test_adapter import make_adapter, message, native_event


async def mention_event(serve, tmp_path, **options):
    msg = options.pop(
        "msg",
        message(threadId="room@chatroom", mentions={"state": "known", "self": True}),
    )
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, msg, **options)
    await adapter.process_native_event(native_event(msg))
    return adapter, queue.get_nowait(), msg, sent


async def test_mention_sends_native_username_once_without_admin_permissions(serve, tmp_path):
    adapter, event, _, sent = await mention_event(serve, tmp_path)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        values = await asyncio.gather(
            *[
                plugin.wechat_mention_member(
                    event, "wxid_diver", "记得看一下群里的消息。"
                )
                for _ in range(3)
            ]
        )
        assert all(json.loads(value)["status"] == "success" for value in values)
        assert len(sent) == 1
        assert sent[0][1] == {
            "threadId": "room@chatroom",
            "text": "记得看一下群里的消息。",
            "mentions": {"userIds": ["wxid_diver"]},
        }
        assert sum(json.loads(value).get("replayed", False) for value in values) == 2
        repeat = json.loads(await plugin.wechat_mention_member(event, "other", "再发一条"))
        assert repeat["replayed"]
        assert repeat["username"] == "wxid_diver"
        assert len(sent) == 1
        assert adapter.state.message_status("m1") == "replied"
        assert event._has_send_oper
        assert not adapter.native_config.enable_history_tools
        assert not adapter.native_config.enable_wechat_actions
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "username,text",
    [("", "hello"), ("all", "hello"), ("other@chatroom", "hello"),
     ("old wang", "hello"), ("a\x00b", "hello"), ("x" * 513, "hello"),
     (None, "hello"), ("wxid_diver", " "), ("wxid_diver", None),
     ("wxid_diver", "x" * 65537)],
)
async def test_invalid_mention_does_not_read_or_send(serve, tmp_path, username, text):
    adapter, event, _, sent = await mention_event(serve, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid mention must not access Native")

    adapter.client._request = forbidden
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(await plugin.wechat_mention_member(event, username, text))
        assert value["status"] == "invalid_request"
        assert sent == []
        assert adapter.state.message_status("m1") == "queued"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("change", ["foreign", "private", "session", "recalled", "cooldown"])
async def test_mentions_cannot_escape_current_live_group(serve, tmp_path, change):
    options = {"msg": message()} if change == "private" else {}
    adapter, event, _, sent = await mention_event(serve, tmp_path, **options)

    async def forbidden(*args, **kwargs):
        pytest.fail("rejected mention must not access Native")

    adapter.client._request = forbidden
    try:
        if change == "foreign":
            event = object()
        elif change == "session":
            event.session.session_id = "a|group|other%40chatroom"
        elif change == "recalled":
            adapter.state.recall("room@chatroom", "900", "m1")
        elif change == "cooldown":
            adapter.loop_guard.trip("room@chatroom", "test")
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert not value["ok"]
        assert sent == []
    finally:
        await adapter.terminate()


async def test_missing_mention_member_does_not_fall_back_to_plain_text(serve, tmp_path):
    adapter, event, _, sent = await mention_event(serve, tmp_path)

    async def unavailable(*args, **kwargs):
        raise module("native_client").GroupMembersUnavailable("failed", "members_unavailable")

    adapter.client.prepare_group_mentions = unavailable
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert not value["ok"]
        assert sent == []
    finally:
        await adapter.terminate()


async def test_unknown_send_outcome_is_not_success_and_cannot_resend(serve, tmp_path):
    adapter, event, _, sent = await mention_event(serve, tmp_path)
    attempts = []

    async def uncertain(thread, text, key, mentions, **kwargs):
        attempts.append(key)
        raise module("native_client").SendError("status_unavailable", key)

    adapter.client.send_text = uncertain
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert value["status"] == "needs_review"
        assert adapter.state.message_status("m1") == "needs_review"
        repeat = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert not repeat["ok"]
        assert len(attempts) == 1
        assert sent == []
    finally:
        await adapter.terminate()


async def test_source_recalled_during_member_lookup_does_not_send(serve, tmp_path):
    adapter, event, msg, sent = await mention_event(serve, tmp_path)

    async def recall(*args, **kwargs):
        msg["recall"] = {"observedAt": "now"}

    adapter.client.prepare_group_mentions = recall
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert not value["ok"]
        assert sent == []
        assert event.is_stopped()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("change", ["disabled", "whitelist", "not_mentioned", "stopped"])
async def test_mention_respects_existing_group_permissions(serve, tmp_path, change):
    adapter, event, msg, sent = await mention_event(serve, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("unauthorized mention must not access Native")

    adapter.client._request = forbidden
    try:
        if change == "disabled":
            adapter.native_config = replace(adapter.native_config, group_reply_mode="disabled")
        elif change == "whitelist":
            adapter.native_config = replace(
                adapter.native_config, allowed_groups=frozenset(["other@chatroom"])
            )
        elif change == "not_mentioned":
            event.message_obj.raw_message["mentions"]["self"] = False
        elif change == "stopped":
            adapter.stopped = True
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = json.loads(
            await plugin.wechat_mention_member(event, "wxid_diver", "记得看一下群里的消息。")
        )
        assert not value["ok"]
        assert sent == []
    finally:
        await adapter.terminate()
