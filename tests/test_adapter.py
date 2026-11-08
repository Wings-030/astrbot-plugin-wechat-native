import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import module
from test_native_client import VALID_JPEG, VALID_PNG, media_asset, member_lookup


def message(**updates):
    data = {
        "id": "m1",
        "accountId": "a",
        "threadId": "peer",
        "serverId": "900",
        "senderId": "user",
        "direction": "incoming",
        "type": 1,
        "body": "你好",
        "createdAt": datetime.now(UTC).isoformat(),
        "version": "1",
        "media": [],
        "mentions": {"state": "known", "self": False, "all": False, "userIds": []},
    }
    data.update(updates)
    return data


def native_event(msg, cursor="1", origin="live"):
    return {
        "cursor": cursor,
        "kind": "message.committed",
        "data": {"messageId": msg["id"], "threadId": msg["threadId"], "origin": origin},
    }


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch):
    from astrbot.core.utils.metrics import Metric

    async def upload(**kwargs):
        pass

    monkeypatch.setattr(Metric, "upload", upload)


async def make_adapter(
    serve,
    tmp_path,
    msg=None,
    *,
    media_data=VALID_JPEG,
    group_details=None,
    send_status=200,
    **options,
):
    msg = msg or message()
    sent = []

    async def handler(req):
        if req.path.endswith("/media") and req.method == "GET":
            return web.json_response(
                {
                    "items": [
                        media_asset(
                            "asset-1",
                            accountId="a",
                            message_id=msg["id"],
                            data=media_data,
                        )
                    ]
                }
            )
        if req.path.endswith("/media/asset-1/content"):
            return web.Response(body=media_data, content_type="image/jpeg")
        if "/messages/" in req.path:
            return web.json_response(msg)
        if "/members/" in req.path and req.method == "GET":
            return web.json_response(member_lookup(req.path.rsplit("/", 1)[-1]))
        if group_details is not None and "/groups/" in req.path:
            group = group_details.get(req.path.rsplit("/", 1)[-1])
            return web.json_response(group or {}, status=200 if group else 404)
        if req.method == "POST":
            sent.append((req.headers["Idempotency-Key"], await req.json()))
            if send_status != 200:
                return web.json_response({"message": "private error details"}, status=send_status)
            return web.json_response(
                {
                    "id": "s1",
                    "state": "accepted",
                    "accountId": "a",
                    "destination": sent[-1][1]["threadId"],
                }
            )
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    cls = module("adapter").WeChatNativeAdapter
    queue = asyncio.Queue()
    cfg = {"id": "native-a", "server_url": url, "account_id": "a", "token": "fake", **options}
    adapter = cls(cfg, {}, queue, state_dir=tmp_path)
    await adapter.client.open()
    adapter.bot_self_id = "bot"
    return adapter, queue, msg, sent


@pytest.mark.parametrize("app_type", [2001, 47])
async def test_group_red_packet_reply_rotation_nickname_cooldown_and_dedup(
    serve, tmp_path, monkeypatch, caplog, app_type
):
    room, other = "room@chatroom", "other@chatroom"
    details = {
        room: {"accountId": "a", "roomId": room, "myDisplayName": "摸鱼小助手"},
        other: {"accountId": "a", "roomId": other},
    }
    now = 100.0
    monkeypatch.setattr(
        module("adapter"), "time", SimpleNamespace(time=time.time, monotonic=lambda: now)
    )
    msg = message(threadId=room, type=49, appMessageType=app_type, body=None)
    adapter, queue, _, sent = await make_adapter(
        serve,
        tmp_path,
        msg,
        group_details=details,
        enable_group_red_packet_reply=True,
        group_reply_style="quote",
    )
    adapter.loop_guard.clock = lambda: now

    async def deliver(cursor, thread=room):
        msg.update(id=f"m{cursor}", serverId=str(900 + cursor), threadId=thread)
        await adapter.process_native_event(native_event(msg, str(cursor)))

    try:
        await deliver(1)
        await deliver(2)
        assert len(sent) == 1
        await deliver(3, other)
        assert [payload["threadId"] for _, payload in sent] == [room, other]
        assert sent[1][1]["text"] in {
            "检测到财富气息，大家手速准备！💰",
            "潜水员集合！水面上漂来一只红包啦～🫧",
            "咚咚咚！给发红包的老板敲三下小鼓！🥁",
        }
        now += 30
        await adapter.process_native_event(native_event(msg, "4"))
        assert len(sent) == 2
        for cursor in range(5, 12):
            await deliver(cursor)
            now += 30
        room_texts = [payload["text"] for _, payload in sent if payload["threadId"] == room]
        assert len(set(room_texts)) == 8
        assert "感谢老板！摸鱼小助手宣布你是今天最帅的人！😎" in room_texts
        details[room]["myDisplayName"] = "新昵称"
        for cursor in range(12, 20):
            await deliver(cursor)
            now += 30
        updated = [payload["text"] for _, payload in sent[-8:]]
        assert len(set(updated)) == 8
        assert "感谢老板！新昵称宣布你是今天最帅的人！😎" in updated
        assert updated[0] != room_texts[-1]
        assert all(set(payload) == {"threadId", "text"} for _, payload in sent)
        assert sum("红包暖场已发送" in rec.message for rec in caplog.records) == len(sent)
        assert queue.empty()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "updates,options,origin",
    [
        ({}, {"enable_group_red_packet_reply": False}, "live"),
        ({}, {"group_reply_mode": "disabled"}, "live"),
        ({}, {"allowed_groups": ["other@chatroom"]}, "live"),
        ({"threadId": "peer"}, {}, "live"),
        ({"senderId": "bot"}, {}, "live"),
        ({"direction": "outgoing"}, {}, "live"),
        ({"recall": {}}, {}, "live"),
        ({"appMessageType": 2000}, {}, "live"),
        ({"type": 47, "appMessageType": None}, {}, "live"),
        ({"type": 1, "body": "恭喜发财，红包 🧧"}, {}, "live"),
        ({}, {}, "history_sync"),
    ],
)
async def test_group_red_packet_reply_ignores_ineligible_events(
    serve, tmp_path, updates, options, origin
):
    raw = {"threadId": "room@chatroom", "type": 49, "appMessageType": 2001, "body": None}
    msg = message(**(raw | updates))
    cfg = {"enable_group_red_packet_reply": True} | options
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg, **cfg)
    try:
        await adapter.process_native_event(native_event(msg, origin=origin))
        assert sent == []
        assert queue.empty()
    finally:
        await adapter.terminate()


async def test_group_red_packet_reply_logs_only_a_safe_send_failure(serve, tmp_path, caplog):
    msg = message(threadId="room@chatroom", type=49, appMessageType=2001)
    adapter, queue, _, sent = await make_adapter(
        serve, tmp_path, msg, group_details={}, send_status=403, enable_group_red_packet_reply=True
    )
    try:
        await adapter.process_native_event(native_event(msg))
        await adapter.process_native_event(native_event(msg, "2"))
        assert len(sent) == 1
        assert queue.empty()
        assert adapter.state.message_status(msg["id"]) == "failed"
        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert warnings == ["WeChat Native 红包暖场发送失败：request_failed"]
        assert "private error details" not in caplog.text
        assert "红包暖场已发送" not in caplog.text
    finally:
        await adapter.terminate()


async def test_private_event_real_framework_lifecycle_and_reply(serve, tmp_path):
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import MessageType

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert isinstance(event, AstrMessageEvent)
        assert event.message_obj.type == MessageType.FRIEND_MESSAGE
        assert event.message_str == "你好"
        assert event.get_sender_id() == "user"
        assert event.platform_meta.id == "native-a"
        assert event.message_obj.self_id == "bot"
        assert event.session.session_id == "a|private|peer"
        await event.send(MessageChain([Plain("回复")]))
        assert event._has_send_oper
        assert sent[0][1] == {"threadId": "peer", "text": "回复"}
        assert adapter.state.message_status("m1") == "replied"
        assert adapter.state.send_record(sent[0][0])["state"] == "accepted"
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
        assert adapter.state.cursor == "2"
    finally:
        await adapter.terminate()
    assert adapter.client.closed


@pytest.mark.parametrize(
    "updates,options,origin",
    [
        ({"direction": "outgoing"}, {}, "live"),
        ({"senderId": "bot"}, {}, "live"),
        ({"type": 3}, {"enable_media": False}, "live"),
        ({"type": 47}, {}, "live"),
        (
            {"threadId": "room@chatroom", "type": 3, "body": ""},
            {"group_reply_mode": "all"},
            "live",
        ),
        ({"recall": {"observedAt": "now"}}, {}, "live"),
        ({}, {}, "history"),
        ({}, {}, "unknown"),
        ({"createdAt": (datetime.now(UTC) - timedelta(days=1)).isoformat()}, {}, "live"),
        ({}, {"enable_private_reply": False}, "live"),
        ({}, {"allowed_users": ["other"]}, "live"),
        ({"threadId": "room@chatroom"}, {}, "live"),
        ({"threadId": "room@chatroom", "mentions": {"state": "unknown", "self": True}}, {}, "live"),
        (
            {
                "threadId": "room@chatroom",
                "mentions": {"state": "known", "self": False, "all": True},
            },
            {},
            "live",
        ),
        ({"threadId": "room@chatroom"}, {"group_reply_mode": "disabled"}, "live"),
        (
            {"threadId": "room@chatroom"},
            {"group_reply_mode": "all", "allowed_groups": ["other"]},
            "live",
        ),
    ],
)
async def test_policy_drops_without_model_dispatch(serve, tmp_path, updates, options, origin):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, message(**updates), **options)
    try:
        await adapter.process_native_event(native_event(msg, origin=origin))
        assert queue.empty()
        assert sent == []
        assert adapter.state.cursor == "1"
    finally:
        await adapter.terminate()


async def test_group_mentions_isolation_and_reply_target(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain

    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.get_group_id() == "room@chatroom"
        assert event.session.session_id == "a|group|room%40chatroom|user"
        assert isinstance(event.message_obj.message[0], At)
        await event.send(MessageChain([At(qq="user"), Plain("answer")]))
        assert sent[0][1] == {
            "threadId": "room@chatroom",
            "text": "answer",
            "mentions": {"userIds": ["user"]},
        }
        msg.update(id="m2", senderId="other")
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.get_nowait().session.session_id != event.session.session_id
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "outcome",
    [
        "accepted",
        "recalled",
        "failed",
        "failed_recalled",
        "explicit_failed",
        "quote_failed",
        "auth_failed",
        "identity_failed",
    ],
)
async def test_new_group_refreshes_before_single_send_and_rechecks_recall(serve, tmp_path, outcome):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain, Reply

    requests = []
    refreshed = False
    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})

    async def serve_new_group(handler):
        async def native(req):
            nonlocal refreshed
            requests.append((req.method, req.path))
            if req.path.endswith("/members/refresh"):
                return web.json_response(
                    {"id": "op1", "accountId": "a", "kind": "metadata", "state": "queued"},
                    status=202,
                )
            if "/operations/" in req.path:
                refreshed = True
                if outcome in {"recalled", "failed_recalled"}:
                    msg["recall"] = {"observedAt": "now"}
                return web.json_response(
                    {
                        "id": "op1",
                        "accountId": "a",
                        "kind": "metadata",
                        "state": "succeeded" if outcome in {"accepted", "recalled"} else "failed",
                        "result": {
                            "code": {
                                "auth_failed": "auth_required",
                                "identity_failed": "account_identity_mismatch",
                            }.get(outcome, "operation_failed"),
                            "details": "fake-token PRIVATE CHAT",
                        },
                    }
                )
            if ("/members/" in req.path or req.path.endswith("/sends")) and not refreshed:
                return web.json_response({"error": {"code": "media_not_cached"}}, status=404)
            if req.path.endswith("/sends") and outcome not in {"accepted", "recalled"}:
                assert "mentions" not in await req.json()
            return await handler(req)

        return await serve(native)

    adapter, queue, _, sent = await make_adapter(serve_new_group, tmp_path, msg)
    adapter.client.poll_interval = 0.001
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        chain = MessageChain().message("answer")
        if outcome == "explicit_failed":
            chain = MessageChain([At(qq="user"), Plain("answer")])
        elif outcome == "quote_failed":
            chain = MessageChain([Reply(id="m1"), Plain("answer")])
        blocked = outcome in {"explicit_failed", "auth_failed", "identity_failed"}
        delivered = outcome in {"accepted", "failed", "quote_failed"}
        if blocked:
            with pytest.raises(ValueError, match="群成员"):
                await event.send(chain)
            assert sent == []
            assert adapter.state.message_status("m1") == "failed"
        else:
            await event.send(chain)
        if outcome in {"recalled", "failed_recalled"}:
            assert sent == []
            assert event.is_stopped()
            assert adapter.state.message_status("m1") == "recalled"
        elif delivered:
            expected = {
                "threadId": "room@chatroom",
                "text": "answer",
            }
            if outcome == "accepted":
                expected["mentions"] = {"userIds": ["user"]}
            elif outcome == "quote_failed":
                expected["quote"] = {"messageId": "m1", "serverId": "900"}
            assert sent[0][1] == expected
            assert adapter.state.message_status("m1") == "replied"
        assert requests.count(("POST", "/v1/accounts/a/sends")) == delivered
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == delivered
        assert requests.count(("POST", "/v1/accounts/a/groups/room@chatroom/members/refresh")) == 1
    finally:
        await adapter.terminate()


async def test_all_mode_shared_session(serve, tmp_path):
    msg = message(threadId="room@chatroom")
    adapter, queue, msg, _ = await make_adapter(
        serve, tmp_path, msg, group_reply_mode="all", unique_session=False
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.session.session_id == "a|group|room%40chatroom"
        # Wake component lets AstrBot's normal waking stage accept explicit all mode.
        assert event.message_obj.message[0].qq == "bot"
    finally:
        await adapter.terminate()


async def test_recall_prevents_queued_reply(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        await event.send(MessageChain().message("should not send"))
        assert event.is_stopped()
        assert not sent
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


async def test_identity_mismatch_keeps_cursor_for_replay(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, message(accountId="foreign"))
    try:
        with pytest.raises(ValueError):
            await adapter.process_native_event(native_event(msg))
        assert adapter.state.cursor == "0"
        assert queue.empty()
    finally:
        await adapter.terminate()


async def test_run_reconnects_from_committed_cursor_and_stops(serve, tmp_path):
    connections = []
    resumed = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            connections.append(req.query["after"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            if len(connections) == 1:
                await response.write(("data: " + json.dumps(native_event(msg)) + "\n\n").encode())
            else:
                resumed.set()
                await release.wait()
            return response
        if "/messages/" in req.path:
            return web.json_response(msg)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"id": "a", "server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(resumed.wait(), 3)
        assert connections == ["0", "1"]
        assert adapter._event_queue.qsize() == 1
        await adapter.terminate()
        await asyncio.wait_for(task, 1)
        assert adapter.client.closed
    finally:
        release.set()
        await adapter.terminate()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_plugin_registers_config_in_real_astrbot():
    from astrbot.core.platform.register import platform_cls_map, platform_registry

    module("main")
    assert "wechat_native" in platform_cls_map
    metadata = next(x for x in platform_registry if x.name == "wechat_native")
    assert metadata.adapter_display_name == "WeChat Native"
    assert not metadata.support_streaming_message
    assert metadata.config_metadata["token_file"]["type"] == "string"
    assert metadata.default_config_tmpl["group_reply_mode"] == "mention"


async def test_platform_icon_is_served_through_astrbot_file_api():
    import httpx
    from astrbot.core.platform.register import platform_registry
    from astrbot.dashboard.api.files import router
    from astrbot.dashboard.services.config_service import ConfigDisplayService
    from astrbot.dashboard.services.file_service import FileService
    from fastapi import FastAPI

    module("main")
    metadata = next(item for item in platform_registry if item.name == "wechat_native")
    service = ConfigDisplayService.__new__(ConfigDisplayService)
    service._logo_token_cache = {}
    templates = {"wechat_native": metadata.default_config_tmpl.copy()}
    await service.register_platform_logo(metadata, templates)
    token = templates["wechat_native"].get("logo_token")
    assert token, "WeChat Native must provide a resolvable platform icon"

    app = FastAPI()
    app.state.services = SimpleNamespace(files=FileService())
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for _ in range(2):  # The list and editor must both be able to fetch the icon.
            response = await client.get(f"/api/v1/files/tokens/{token}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("image/png")
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


async def test_startup_retries_and_preserves_framework_instance_identity(serve, tmp_path):
    attempts = 0
    connected = asyncio.Event()
    release = asyncio.Event()

    async def handler(req):
        nonlocal attempts
        if req.path == "/v1/status":
            return web.json_response({"serverId": "test-native-server"})
        if req.path.endswith("events/stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(req)
            connected.set()
            await release.wait()
            return response
        attempts += 1
        if attempts == 1:
            return web.Response(status=503)
        return web.json_response(
            {"id": "a", "ownUsername": "bot", "authenticated": True, "connection": "online"}
        )

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    original_identity = adapter.client_self_id
    adapter.reconnect_delay = 0.001
    task = asyncio.create_task(adapter.run())
    try:
        await asyncio.wait_for(connected.wait(), 1)
        assert attempts == 2
        assert adapter.client_self_id == original_identity
        assert adapter.client_self_id != "a"
        assert adapter.bot_self_id == "bot"
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_sends_stop_after_unknown_receipt(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    posts = []
    msg = message()

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "needs_review", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    first = second = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        first = asyncio.create_task(event.send(MessageChain().message("first")))
        await entered.wait()
        second = asyncio.create_task(event.send(MessageChain().message("second")))
        await asyncio.sleep(0)  # both calls pass the initial gate before first receipt
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, Exception) for result in results)
        assert len(posts) == 1
        assert adapter.state.message_status("m1") == "needs_review"
    finally:
        release.set()
        await adapter.terminate()


async def test_recall_arriving_during_source_read_blocks_submission(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()
    reads = 0
    posts = []

    async def handler(req):
        nonlocal reads
        if req.method == "POST":
            posts.append(await req.json())
            return web.json_response(
                {"id": "s", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        reads += 1
        if reads == 2:
            entered.set()
            await release.wait()
        return web.json_response(msg)  # GET snapshot predates recall

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        task = asyncio.create_task(event.send(MessageChain().message("late reply")))
        await entered.wait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        release.set()
        await task
        assert posts == []
        assert event.is_stopped()
    finally:
        release.set()
        await adapter.terminate()


async def test_invalid_image_output_failure_is_retained_without_send(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(b"invalid image")]))
        assert not sent
        assert adapter.state.message_status("m1") == "failed"
    finally:
        await adapter.terminate()


def test_metadata_validates_with_astrbot_loader():
    from astrbot.core.star.star_manager import PluginManager
    from conftest import ROOT

    metadata = PluginManager._load_plugin_metadata(str(ROOT))
    assert metadata.name == "astrbot_plugin_wechat_native"
    assert metadata.display_name == "WeChat Native"
    assert metadata.author == "neozhu"


async def test_old_recall_identity_does_not_stop_current_event(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "899"},
            }
        )
        assert not event.is_stopped()
        assert adapter.state.message_status("m1") == "queued"
    finally:
        await adapter.terminate()


async def test_stop_during_pending_send_retains_uncertainty(serve, tmp_path):
    from astrbot.api.event import MessageChain

    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message()

    async def handler(req):
        if req.method == "POST":
            entered.set()
            await release.wait()
            return web.json_response(
                {"id": "s1", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    await adapter.process_native_event(native_event(msg))
    event = adapter._event_queue.get_nowait()
    task = asyncio.create_task(event.send(MessageChain().message("pending")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(adapter.terminate(), adapter.terminate())
        assert task.done()
        assert adapter.client.closed
        path = await asyncio.to_thread(lambda: next(tmp_path.glob("*.sqlite3")))
        state = module("state").StateStore(path)
        assert state.message_status("m1") == "needs_review"
        assert state.db.execute("SELECT state FROM sends").fetchone()[0] == "needs_review"
        state.close()
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_send_by_persisted_session_and_foreign_account_gate(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageType
    from astrbot.core.platform.message_session import MessageSession

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    try:
        good = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "a|private|peer")
        await adapter.send_by_session(good, MessageChain().message("proactive"))
        assert sent[0][1] == {"threadId": "peer", "text": "proactive"}
        bad = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "other|private|peer")
        with pytest.raises(ValueError):
            await adapter.send_by_session(bad, MessageChain().message("bad"))
        assert len(sent) == 1
    finally:
        await adapter.terminate()


async def test_streaming_output_is_aggregated_once(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()

        async def chunks():
            yield MessageChain().message("你")
            yield MessageChain().message("好")

        await event.send_streaming(chunks())
        assert [payload for _, payload in sent] == [{"threadId": "peer", "text": "你好"}]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "body,mentions,wanted",
    [
        ("@机器人\u2005/help", {"state": "known", "self": True}, "/help"),
        ("@机器人\u2005 /help", {"state": "known", "self": True}, "/help"),
        ("@成员\u2005@机器人\u2005/help", {"state": "known", "self": True}, "/help"),
        ("hello @机器人\u2005/help", {"state": "known", "self": True}, "hello @机器人\u2005/help"),
        ("@机器人 /help", {"state": "known", "self": True}, "@机器人 /help"),
        ("@机器人\u2005/help", {"state": "unknown", "self": True}, "@机器人\u2005/help"),
        ("@成员\u2005/help", {"state": "known", "self": False}, "@成员\u2005/help"),
    ],
)
async def test_group_mention_prefix_normalization_keeps_raw_body(
    serve, tmp_path, body, mentions, wanted
):
    from astrbot.api.message_components import Plain

    msg = message(threadId="room@chatroom", body=body, mentions=mentions)
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg, group_reply_mode="all")
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        assert event.message_str == wanted
        assert event.message_obj.raw_message["body"] == body
        assert isinstance(event.message_obj.message[-1], Plain)
        assert event.message_obj.message[-1].text == wanted
        if wanted == "/help":
            from astrbot.core.star.filter.command import CommandFilter

            command = CommandFilter("/help")
            command.handler_params = {}
            event.is_at_or_wake_command = True
            assert command.filter(event, {})
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("offset", [-600, 120])
async def test_live_time_rejections_warn_without_content_and_are_rate_limited(
    serve, tmp_path, monkeypatch, offset
):
    now = datetime.now(UTC).replace(microsecond=0)
    msg = message(body="PRIVATE BODY", createdAt=(now + timedelta(seconds=offset)).isoformat())
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    clock = [0.0]
    warnings = []
    adapter_module = module("adapter")
    monkeypatch.setattr(
        adapter_module,
        "time",
        SimpleNamespace(time=lambda: now.timestamp(), monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(
        adapter_module.logger, "warning", lambda text, *args: warnings.append(text % args)
    )
    try:
        await adapter.process_native_event(native_event(msg))
        msg["id"] = "m2"
        await adapter.process_native_event(native_event(msg, "2"))
        assert len(warnings) == 1
        clock[0] = 61
        msg["id"] = "m3"
        await adapter.process_native_event(native_event(msg, "3"))
        assert len(warnings) == 2
        assert "PRIVATE BODY" not in " ".join(warnings)
        assert str(abs(offset)) in warnings[0]
        assert queue.empty()
        assert adapter.state.cursor == "3"
        assert adapter.state.message_status("m1") == "ignored"
    finally:
        await adapter.terminate()


async def test_empty_output_is_no_reply_across_restart(serve, tmp_path):
    from astrbot.api.event import MessageChain

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        await queue.get_nowait().send(MessageChain().message(" \n\t"))
        assert adapter.state.message_status("m1") == "no_reply"
        assert not sent
    finally:
        await adapter.terminate()
    state = module("state").StateStore(next(tmp_path.glob("*.sqlite3")))
    try:
        assert state.message_status("m1") == "no_reply"
    finally:
        state.close()


async def test_split_group_reply_only_auto_mentions_first_successful_segment(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain

    msg = message(threadId="room@chatroom", mentions={"state": "known", "self": True})
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain().message(" "))
        await event.send(MessageChain().message("first"))
        await event.send(MessageChain().message("second"))
        await event.send(MessageChain([At(qq="other"), Plain("explicit")]))
        assert [payload.get("mentions") for _, payload in sent] == [
            {"userIds": ["user"]},
            None,
            {"userIds": ["other"]},
        ]
        msg["recall"] = {"observedAt": "now"}
        await event.send(MessageChain().message("recalled segment"))
        assert len(sent) == 3
        assert event.is_stopped()
    finally:
        await adapter.terminate()


async def test_later_validation_failure_keeps_partial_success_and_blocks_next_segment(
    serve, tmp_path
):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain().message("first"))
        with pytest.raises(ValueError):
            await event.send(MessageChain([Image.fromBytes(b"invalid image")]))
        assert adapter.state.message_status("m1") == "partial"
        assert adapter.state.send_record(sent[0][0])["state"] == "accepted"
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("third"))
        assert len(sent) == 1
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("failure_state", ["failed", "needs_review"])
async def test_later_receipt_failure_keeps_partial_success(serve, tmp_path, failure_state):
    from astrbot.api.event import MessageChain

    msg = message()
    posts = []

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            return web.json_response(
                {
                    "id": "s" + str(len(posts)),
                    "accountId": "a",
                    "destination": "peer",
                    "state": "accepted" if len(posts) == 1 else failure_state,
                }
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        await event.send(MessageChain().message("first"))
        with pytest.raises(module("native_client").SendError):
            await event.send(MessageChain().message("second"))
        assert adapter.state.message_status("m1") == "partial"
        assert [row[0] for row in adapter.state.db.execute("SELECT state FROM sends")] == [
            "accepted",
            failure_state,
        ]
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("third"))
        assert len(posts) == 2
    finally:
        await adapter.terminate()


async def test_unopened_proactive_send_does_not_create_outbox_entry(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.platform import MessageType
    from astrbot.core.platform.message_session import MessageSession

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    await adapter.client.close()
    session = MessageSession("native-a", MessageType.FRIEND_MESSAGE, "a|private|peer")
    try:
        with pytest.raises(ValueError, match="未连接"):
            await adapter.send_by_session(session, MessageChain().message("proactive"))
        assert adapter.state.db.execute("SELECT count(*) FROM sends").fetchone()[0] == 0
        assert not sent
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("earlier_reply", [False, True])
async def test_cancelled_waiting_segment_cannot_be_cleared_by_inflight_success(
    serve, tmp_path, earlier_reply
):
    from astrbot.api.event import MessageChain

    entered, release = asyncio.Event(), asyncio.Event()
    msg, posts = message(), []

    async def handler(req):
        if req.method == "POST":
            posts.append(await req.json())
            send_id = "s" + str(len(posts))
            if len(posts) == (2 if earlier_reply else 1):
                entered.set()
                await release.wait()
            return web.json_response(
                {"id": send_id, "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    inflight = waiting = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = adapter._event_queue.get_nowait()
        if earlier_reply:
            await event.send(MessageChain().message("earlier"))
        inflight = asyncio.create_task(event.send(MessageChain().message("inflight")))
        await asyncio.wait_for(entered.wait(), 2)
        waiting = asyncio.create_task(event.send(MessageChain().message("waiting")))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert adapter.state.message_status("m1") == (
            "partial" if earlier_reply else "needs_review"
        )
        release.set()
        await inflight
        assert adapter.state.message_status("m1") == "partial"
        with pytest.raises(RuntimeError, match="人工检查"):
            await event.send(MessageChain().message("must not continue"))
        assert len(posts) == (2 if earlier_reply else 1)
    finally:
        release.set()
        await adapter.terminate()
        await asyncio.gather(
            *(task for task in (inflight, waiting) if task), return_exceptions=True
        )


@pytest.mark.parametrize(
    "payload,body", [(VALID_JPEG, ""), (VALID_PNG, None), (VALID_JPEG, "看图")]
)
async def test_private_direct_image_delivers_astrbot_image_and_default_prompt(
    serve, tmp_path, payload, body
):
    from astrbot.api.message_components import Image, Plain

    msg = message(type=3, body=body)
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg, media_data=payload)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await asyncio.wait_for(queue.get(), 1)
        expected_text = "看图" if body == "看图" else "请描述这张图片的内容。"
        assert event.message_str == expected_text
        components = event.get_messages()
        assert [type(c) for c in components] == [Plain, Image]
        assert components[0].text == expected_text
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "status,code,expected_reply",
    [
        (404, "media_not_available", "图片无法读取，请重新发送。"),
        (413, "image_size_limit", "图片太大，暂时无法处理。"),
        (403, "insufficient_scope", "图片读取功能未正确配置，请联系管理员。"),
        (422, "image_format_unsupported", "暂不支持这种图片格式。"),
    ],
)
async def test_media_errors_send_concise_reply_without_llm_dispatch(
    serve, tmp_path, status, code, expected_reply
):
    msg = message(type=3, body="")
    sent = []

    async def handler(req):
        if req.path.endswith("/media"):
            return web.json_response({"error": {"code": code}}, status=status)
        if "/messages/" in req.path:
            return web.json_response(msg)
        if req.method == "POST" and req.path.endswith("/sends"):
            sent.append(await req.json())
            return web.json_response(
                {"id": "s1", "state": "accepted", "accountId": "a", "destination": "peer"}
            )
        return web.Response(status=404)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.gather(*list(adapter._media_tasks))
        assert adapter._event_queue.empty()
        assert sent == [{"threadId": "peer", "text": expected_reply}]
        assert adapter.state.message_status("m1") == "replied"
    finally:
        await adapter.terminate()


async def test_triggering_image_recall_during_download_silently_cancels(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message(type=3, body="")
    sent = []

    async def handler(req):
        if req.path.endswith("/media"):
            entered.set()
            await release.wait()
            return web.json_response(
                {"items": [media_asset("a1", accountId="a", message_id="m1", data=VALID_JPEG)]}
            )
        if req.method == "POST":
            sent.append(await req.json())
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"messageId": "m1", "threadId": "peer", "serverId": "900"},
            }
        )
        release.set()
        await asyncio.gather(*list(adapter._media_tasks), return_exceptions=True)
        assert adapter._event_queue.empty()
        assert sent == []
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        release.set()
        await adapter.terminate()


async def test_slow_media_download_does_not_block_concurrent_text_messages(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    img_msg = message(id="m1", serverId="900", type=3, body="")
    txt_msg = message(id="m2", serverId="901", type=1, body="普通文本")

    async def handler(req):
        if req.path.endswith("/messages/m1/media"):
            entered.set()
            await release.wait()
            return web.json_response(
                {"items": [media_asset("a1", accountId="a", message_id="m1", data=VALID_JPEG)]}
            )
        if req.path.endswith("/media/a1/content"):
            return web.Response(body=VALID_JPEG, content_type="image/jpeg")
        if req.path.endswith("/messages/m1"):
            return web.json_response(img_msg)
        if req.path.endswith("/messages/m2"):
            return web.json_response(txt_msg)
        return web.Response(status=404)

    url = await serve(handler)
    queue = asyncio.Queue()
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        queue,
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(img_msg, "1"))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.process_native_event(native_event(txt_msg, "2"))
        first_event = queue.get_nowait()
        assert first_event.message_str == "普通文本"
        release.set()
        second_event = await asyncio.wait_for(queue.get(), 1)
        assert second_event.message_str == "请描述这张图片的内容。"
    finally:
        release.set()
        await adapter.terminate()


async def test_terminate_during_media_download_marks_needs_review_across_restart(serve, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    msg = message(type=3, body="")

    async def handler(req):
        if req.path.endswith("/media"):
            entered.set()
            await release.wait()
        return web.json_response(msg)

    url = await serve(handler)
    adapter = module("adapter").WeChatNativeAdapter(
        {"server_url": url, "account_id": "a", "token": "fake"},
        {},
        asyncio.Queue(),
        state_dir=tmp_path,
    )
    await adapter.client.open()
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.terminate()
        state = module("state").StateStore(next(tmp_path.glob("*.sqlite3")))
        try:
            assert state.message_status("m1") == "needs_review"
        finally:
            state.close()
    finally:
        release.set()
        await adapter.terminate()
