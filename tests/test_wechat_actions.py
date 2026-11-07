import asyncio
import json

import pytest
from aiohttp import web
from conftest import module
from test_adapter import message, native_event
from test_history_tools import tool_event


def operation(state="succeeded", **changes):
    data = {
        "id": "op1",
        "accountId": "a",
        "kind": "group_mutation",
        "state": state,
        "result": {
            "action": "my_name",
            "targetId": "room@chatroom",
            "groupId": "room@chatroom",
            "remoteOutcome": "accepted",
            "profileProjection": "partial",
        },
    }
    data.update(changes)
    return data


async def action_event(serve, tmp_path, **options):
    admin_file = tmp_path / "admin-token.txt"
    admin_file.write_text("fake-admin")
    return await tool_event(
        serve,
        tmp_path,
        enable_wechat_actions=True,
        admin_token_file=str(admin_file),
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
        **options,
    )


async def test_nickname_submits_once_with_durable_intent_and_no_confirmation(serve, tmp_path):
    adapter, event, _ = await action_event(serve, tmp_path)
    requests = []

    async def handler(req):
        if req.method == "PUT":
            assert adapter.state.action_intent("a", "room@chatroom", "m1") is not None
            requests.append(
                (req.method, req.path, await req.json(), req.headers["Idempotency-Key"])
            )
        return web.json_response(operation())

    url = await serve(handler)
    try:
        adapter.admin_client.api_url = url
        plugin = object.__new__(module("main").WeChatNativePlugin)
        results = await asyncio.gather(
            *[plugin.wechat_set_my_group_nickname(event, "  摸鱼小助手  ") for _ in range(3)]
        )
        assert all(json.loads(r)["status"] == "success" for r in results)
        assert len(requests) == 1
        assert requests[0][:3] == (
            "PUT",
            "/accounts/a/groups/room@chatroom/my-name",
            {"displayName": "摸鱼小助手"},
        )
        assert "fake-admin" not in "".join(results)
        assert json.loads(await plugin.wechat_set_my_group_nickname(event, "other"))["ok"]
        assert len(requests) == 1
    finally:
        await adapter.terminate()


async def test_pending_and_lost_response_never_resubmit(serve, tmp_path):
    adapter, event, _ = await action_event(serve, tmp_path)
    calls = []

    async def handler(req):
        calls.append(req.method)
        if req.method == "PUT":
            return web.json_response(operation("queued"), status=202)
        return web.json_response(operation("running"))

    adapter.admin_client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        assert (
            json.loads(await plugin.wechat_set_my_group_nickname(event, "new"))["status"]
            == "pending"
        )
        assert (
            json.loads(await plugin.wechat_set_my_group_nickname(event, "new"))["status"]
            == "pending"
        )
        assert calls.count("PUT") == 1
    finally:
        await adapter.terminate()


async def test_timeout_recovers_by_original_key_after_restart(serve, tmp_path):
    adapter, event, _ = await action_event(serve, tmp_path)
    mutations = []
    recovered = [False]

    async def handler(req):
        if req.method == "PUT":
            mutations.append(req.headers["Idempotency-Key"])
            return web.json_response({}, status=503)
        if not recovered[0]:
            return web.json_response({}, status=404)
        assert req.query["key"] == mutations[0]
        return web.json_response(operation())

    url = await serve(handler)
    adapter.admin_client.api_url = url
    plugin = object.__new__(module("main").WeChatNativePlugin)
    try:
        first = json.loads(await plugin.wechat_set_my_group_nickname(event, "new"))
        assert first["status"] == "needs_review"
        await plugin.wechat_set_my_group_nickname(event, "new")
        assert len(mutations) == 1
        cfg = dict(adapter.config)
        await adapter.terminate()
        adapter = module("adapter").WeChatNativeAdapter(
            cfg, {}, asyncio.Queue(), state_dir=tmp_path
        )
        adapter.bot_self_id = "bot"
        adapter.admin_client.api_url = url
        recovered[0] = True
        await adapter.actions.recover()
        assert adapter.state.action_intent("a", "room@chatroom", "m1")["status"] == "success"
        assert len(mutations) == 1
    finally:
        await adapter.terminate()


async def test_nickname_validation_rate_limit_and_identity_conflict(serve, tmp_path):
    adapter, event, msg = await action_event(serve, tmp_path)
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response(operation())

    adapter.admin_client.api_url = await serve(handler)
    plugin = object.__new__(module("main").WeChatNativePlugin)
    try:
        for name in (" ", "a\x00b", "中" * 171):
            assert (
                json.loads(await plugin.wechat_set_my_group_nickname(event, name))["status"]
                == "invalid_request"
            )
        assert not requests
        assert json.loads(await plugin.wechat_set_my_group_nickname(event, "new"))["ok"]
        msg.update(id="m2", serverId="901")
        await adapter.process_native_event(native_event(msg, "2"))
        event2 = (
            adapter._events["m2"] if "m2" in adapter._events else adapter._event_queue.get_nowait()
        )
        assert (
            json.loads(await plugin.wechat_set_my_group_nickname(event2, "next"))["status"]
            == "rate_limited"
        )
        assert requests == ["PUT"]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "admins,status,code,expected",
    [
        ([], 200, "", "permission_denied"),
        (["user"], 403, "permission_denied", "permission_denied"),
        (["user"], 403, "insufficient_scope", "configuration_error"),
        (["user"], 403, "forbidden", "configuration_error"),
        (["user"], 200, "", "success"),
    ],
)
async def test_rename_checks_sender_scope_and_remote_permission(
    serve,
    tmp_path,
    admins,
    status,
    code,
    expected,
):
    adapter, event, _ = await action_event(serve, tmp_path, wechat_action_admins=admins)
    requests = []

    async def handler(req):
        requests.append((req.method, await req.json()))
        if status != 200:
            return web.json_response(
                {"error": {"code": code, "message": "secret token"}}, status=status
            )
        op = operation()
        op["result"]["action"] = "rename"
        return web.json_response(op)

    adapter.admin_client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_rename_group(event, "快乐摸鱼总部"))
        assert data["status"] == expected
        assert "secret" not in json.dumps(data)
        assert requests == ([("PATCH", {"name": "快乐摸鱼总部"})] if admins else [])
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({}, "success"),
        ({"direction": "incoming"}, "not_recallable"),
        ({"threadId": "other"}, "permission_denied"),
        ({"accountId": "other"}, "permission_denied"),
        ({"senderId": "other"}, "not_recallable"),
        ({"serverId": None}, "not_recallable"),
        ({"serverId": 123}, "not_recallable"),
        ({"recall": {}}, "already_recalled"),
    ],
)
async def test_recall_only_current_chat_own_exact_message(serve, tmp_path, updates, expected):
    adapter, event, _ = await action_event(serve, tmp_path, wechat_action_admins=["user"])
    target = message(
        id="own1",
        threadId="room@chatroom",
        senderId="bot",
        direction="outgoing",
        serverId="18446744073709551615",
    )
    target.update(updates)
    requests = []

    async def handler(req):
        requests.append((req.method, req.path))
        if req.method == "GET":
            return web.json_response(target)
        assert await req.json() == {"serverId": "18446744073709551615"}
        return web.json_response(
            {
                "id": "recall1",
                "accountId": "a",
                "kind": "message_recall",
                "state": "succeeded",
                "result": {
                    "messageId": "own1",
                    "threadId": "room@chatroom",
                    "serverId": "18446744073709551615",
                    "status": "recalled",
                    "remoteOutcome": "confirmed",
                    "profileProjection": "complete",
                },
            }
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_recall_own_message(event, "own1"))
        assert data["status"] == expected
        assert sum(method == "POST" for method, _ in requests) == (
            1 if expected == "success" else 0
        )
        if expected == "success":
            assert json.loads(await plugin.wechat_recall_own_message(event, "own1"))["ok"]
            assert sum(method == "POST" for method, _ in requests) == 1
    finally:
        await adapter.terminate()


async def test_recall_recent_ambiguity_does_not_submit(serve, tmp_path):
    adapter, event, _ = await action_event(serve, tmp_path, wechat_action_admins=["user"])
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response(
            {
                "items": [
                    message(
                        id="own1", threadId="room@chatroom", direction="outgoing", senderId="bot"
                    ),
                    message(
                        id="own2", threadId="room@chatroom", direction="outgoing", senderId="bot"
                    ),
                ],
                "nextCursor": None,
            }
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        assert (
            json.loads(await plugin.wechat_recall_own_message(event))["status"]
            == "ambiguous_target"
        )
        assert requests == ["GET"]
    finally:
        await adapter.terminate()


async def test_recall_pending_lost_response_and_private_scope(serve, tmp_path):
    adapter, event, _ = await tool_event(
        serve,
        tmp_path,
        enable_wechat_actions=True,
        wechat_action_admins=["user"],
    )
    posts = []

    async def handler(req):
        if req.method == "GET":
            return web.json_response(message(id="own1", direction="outgoing", senderId="bot"))
        posts.append(await req.json())
        return web.json_response({}, status=503)

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        for _ in range(2):
            data = json.loads(await plugin.wechat_recall_own_message(event, "own1"))
            assert data["status"] == "needs_review"
        assert posts == [{"serverId": "900"}]
        assert adapter.admin_client is None  # recall uses the read/send credential
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("recall_identity_mismatch", "needs_review"),
        ("recall_target_changed", "needs_review"),
        ("recall_target_unavailable", "not_recallable"),
    ],
)
async def test_native_recall_errors_preserve_safe_business_status(serve, tmp_path, code, expected):
    adapter, event, _ = await action_event(serve, tmp_path, wechat_action_admins=["user"])
    posts = []

    async def handler(req):
        if req.method == "GET":
            return web.json_response(
                message(id="own1", threadId="room@chatroom", direction="outgoing", senderId="bot")
            )
        posts.append(True)
        return web.json_response({"error": {"code": code, "message": "secret"}}, status=409)

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        for _ in range(2):
            value = await plugin.wechat_recall_own_message(event, "own1")
            assert json.loads(value)["status"] == expected
            assert "secret" not in value
        assert len(posts) == 1
        assert adapter.state.action_intent("a", "room@chatroom", "m1")["status"] == expected
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "change",
    [
        {"accountId": "wrong"},
        {"kind": "wrong"},
        {"state": "unknown"},
        {"id": 123},
        {"result": {"action": "my_name", "targetId": "other"}},
    ],
)
async def test_untrusted_operation_identity_never_reports_success(serve, tmp_path, change):
    adapter, event, _ = await action_event(serve, tmp_path)
    mutations = []

    async def handler(req):
        if req.method == "PUT":
            mutations.append(True)
        return web.json_response(operation(**change))

    adapter.admin_client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        for _ in range(2):
            data = json.loads(await plugin.wechat_set_my_group_nickname(event, "new"))
            assert data["status"] == "needs_review"
            assert "operationId" not in data
        assert len(mutations) == 1
    finally:
        await adapter.terminate()


async def test_known_recall_operation_is_polled_without_resubmitting(serve, tmp_path):
    adapter, event, _ = await action_event(serve, tmp_path, wechat_action_admins=["user"])
    methods = []

    async def handler(req):
        methods.append(req.method)
        if "/messages/" in req.path and req.method == "GET":
            return web.json_response(
                message(id="own1", threadId="room@chatroom", direction="outgoing", senderId="bot")
            )
        return web.json_response(
            {
                "id": "recall1",
                "accountId": "a",
                "kind": "message_recall",
                "state": "queued" if req.method == "POST" else "succeeded",
                "result": {
                    "messageId": "own1",
                    "threadId": "room@chatroom",
                    "serverId": "900",
                    "status": "pending" if req.method == "POST" else "recalled",
                    "remoteOutcome": "not_attempted" if req.method == "POST" else "accepted",
                    "profileProjection": "pending" if req.method == "POST" else "complete",
                },
            },
            status=202 if req.method == "POST" else 200,
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        assert (
            json.loads(await plugin.wechat_recall_own_message(event, "own1"))["status"] == "pending"
        )
        assert (
            json.loads(await plugin.wechat_recall_own_message(event, "own1"))["status"] == "success"
        )
        assert methods == ["GET", "POST", "GET"]
    finally:
        await adapter.terminate()
