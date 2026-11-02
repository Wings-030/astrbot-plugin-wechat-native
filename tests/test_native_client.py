import asyncio
import json

import pytest
from aiohttp import web
from conftest import module


def client(url, **kwargs):
    return module("native_client").NativeClient(url + "/v1", "account/a", "fake-token", **kwargs)


def receipt(state="accepted", **kwargs):
    return {
        "id": "s1",
        "accountId": "account/a",
        "destination": "room@chatroom",
        "state": state,
        "serverId": "9007199254740999",
        "errorCode": None,
        **kwargs,
    }


async def test_authenticated_encoded_requests_and_polling(serve):
    seen = []

    async def handler(req):
        assert req.headers["Authorization"] == "Bearer fake-token"
        seen.append((req.method, req.raw_path))
        if req.method == "POST":
            assert req.headers["Idempotency-Key"] == "stable-key"
            assert await req.json() == {
                "threadId": "room@chatroom",
                "text": "hello",
                "mentions": {"userIds": ["u"]},
            }
            return web.json_response(receipt("queued"), status=202)
        if "/sends/" in req.path:
            return web.json_response(receipt())
        return web.json_response({"id": "m/1"})

    url = await serve(handler)
    async with client(url, poll_interval=0.001) as c:
        await c.accounts()
        await c.account()
        await c.message("m/1")
        result = await c.send_text("room@chatroom", "hello", "stable-key", {"userIds": ["u"]})
    assert result["state"] == "accepted"
    assert ("GET", "/v1/accounts/account%2Fa/messages/m%2F1") in seen
    assert sum(method == "POST" for method, _ in seen) == 1
    assert c.closed


@pytest.mark.parametrize("state", ["needs_review", "failed", "cancelled", "unknown"])
async def test_terminal_send_errors_are_never_reposted(serve, state):
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response(receipt(state))

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.SendError) as error:
            await c.send_text("room@chatroom", "hello", "k")
    assert error.value.state == state
    assert requests == ["POST"]


async def test_submission_uncertainty_recovers_only_by_original_key(serve):
    requests = []

    async def handler(req):
        requests.append((req.method, req.path))
        if req.method == "POST":
            return web.json_response({"error": {"code": "timeout"}}, status=503)
        assert req.query["key"] == "original-key"
        return web.json_response(receipt())

    url = await serve(handler)
    async with client(url) as c:
        assert (await c.send_text("room@chatroom", "hi", "original-key"))["id"] == "s1"
    assert requests == [
        ("POST", "/v1/accounts/account/a/sends"),
        ("GET", "/v1/accounts/account/a/sends/by-idempotency-key"),
    ]


async def test_cross_thread_receipt_rejected(serve):
    async def handler(req):
        return web.json_response(receipt(destination="foreign"))

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.SendError, match="receipt_identity_mismatch"):
            await c.send_text("room@chatroom", "hi", "k")


async def test_sse_chunking_heartbeat_unicode_and_resume(serve):
    async def handler(req):
        assert req.query["after"] == "9007199254740999"
        assert req.headers["Last-Event-ID"] == "9007199254740999"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(req)
        data = {
            "cursor": "9007199254741000",
            "kind": "message.committed",
            "data": {"messageId": "消息", "threadId": "p", "origin": "live"},
        }
        raw = (
            ": heartbeat\r\n\r\nid: 9007199254741000\r\ndata: "
            + json.dumps(data, ensure_ascii=False)
            + "\r\n\r\n"
        ).encode()
        for i in range(0, len(raw), 3):
            await response.write(raw[i : i + 3])
        return response

    url = await serve(handler)
    async with client(url) as c:
        events = [event async for event in c.events("9007199254740999")]
    assert len(events) == 1
    assert events[0]["data"]["messageId"] == "消息"
    assert events[0]["cursor"] == "9007199254741000"


async def test_sse_cursor_expired_error_is_actionable(serve):
    async def handler(req):
        return web.Response(
            text='event: error\ndata: {"error":{"code":"event_cursor_expired"}}\n\n',
            content_type="text/event-stream",
        )

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.NativeAPIError) as error:
            async for _ in c.events("0"):
                pass
    assert error.value.status == 410
    assert error.value.code == "event_cursor_expired"


async def test_no_redirect_token_leak_or_error_body_in_exception(serve):
    hits = []

    async def target(req):
        hits.append(req.path)
        return web.json_response({})

    target_url = await serve(target)

    async def handler(req):
        return web.Response(
            status=302, headers={"Location": target_url + "/secret"}, text="fake-token PRIVATE CHAT"
        )

    url = await serve(handler)
    api = module("native_client")
    async with client(url) as c:
        with pytest.raises(api.NativeAPIError) as error:
            await c.accounts()
    assert hits == []
    assert "fake-token" not in str(error.value)
    assert "PRIVATE CHAT" not in str(error.value)


async def test_send_poll_deadline(serve):
    async def handler(req):
        return web.json_response(receipt("submitting"))

    url = await serve(handler)
    api = module("native_client")
    async with client(url, send_timeout=0.01, poll_interval=0.001) as c:
        with pytest.raises(api.SendError) as error:
            await asyncio.wait_for(c.send_text("room@chatroom", "hi", "k"), 1)
    assert error.value.state == "submitting"


async def test_unopened_client_rejects_send_before_submission(serve):
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.json_response(receipt())

    c = client(await serve(handler))
    with pytest.raises(ValueError, match="未连接"):
        await c.send_text("room@chatroom", "hello", "stable-key")
    assert requests == []


async def test_sse_accepts_large_line_within_frame_limit(serve):
    data = {"cursor": "1", "kind": "probe", "data": {"padding": "x" * (700 * 1024)}}

    async def handler(req):
        return web.Response(
            text="data: " + json.dumps(data) + "\n\n", content_type="text/event-stream"
        )

    async with client(await serve(handler)) as c:
        assert [event async for event in c.events("0")] == [data]


async def test_sse_oversized_line_is_protocol_error(serve):
    async def handler(req):
        return web.Response(
            body=b"data: " + b"x" * (1024 * 1024) + b"\n\n", content_type="text/event-stream"
        )

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(api.NativeAPIError) as error:
            async for _ in c.events("0"):
                pass
    assert error.value.status == 502


async def test_sse_comment_only_heartbeats_do_not_accumulate_frame_size(serve):
    data = {"cursor": "1", "kind": "probe", "data": {}}

    async def handler(req):
        return web.Response(
            text=": keepalive\n" * 96000 + "data: " + json.dumps(data) + "\n\n",
            content_type="text/event-stream",
        )

    async with client(await serve(handler)) as c:
        assert [event async for event in c.events("0")] == [data]


async def test_receipt_poll_retries_transient_get_without_resubmitting(serve):
    requests = []

    async def handler(req):
        requests.append(req.method)
        if req.method == "POST":
            return web.json_response(receipt("queued"), status=202)
        if requests.count("GET") == 1:
            return web.Response(status=503)
        return web.json_response(receipt())

    async with client(await serve(handler), poll_interval=0.001, send_timeout=1) as c:
        assert (await c.send_text("room@chatroom", "hi", "stable-key"))["state"] == "accepted"
    assert requests == ["POST", "GET", "GET"]


async def test_receipt_poll_transient_failures_remain_bounded(serve):
    requests = []

    async def handler(req):
        requests.append(req.method)
        if req.method == "POST":
            return web.json_response(receipt("queued"), status=202)
        return web.Response(status=503)

    api = module("native_client")
    async with client(await serve(handler), poll_interval=0.001, send_timeout=0.05) as c:
        with pytest.raises(api.SendError) as error:
            await asyncio.wait_for(c.send_text("room@chatroom", "hi", "stable-key"), 1)
    assert error.value.state == "status_unavailable"
    assert error.value.send_id == "s1"
    assert requests.count("POST") == 1
    assert requests.count("GET") >= 2


async def test_rate_limited_submission_with_missing_lookup_remains_unknown(serve):
    requests = []

    async def handler(req):
        requests.append(req.method)
        return web.Response(status=429 if req.method == "POST" else 404)

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(api.SendError) as error:
            await c.send_text("room@chatroom", "hi", "stable-key")
    assert error.value.state == "submission_unknown"
    assert requests == ["POST", "GET"]


def member_lookup(username, state="present", **updates):
    return {
        "groupId": "room@chatroom",
        "username": username,
        "state": state,
        "member": {"username": username} if state == "present" else None,
        "coverage": "cached",
        **updates,
    }


@pytest.mark.parametrize("missing", ["group", "bot", "user", None])
async def test_prepare_mentions_refreshes_only_missing_group_members(serve, missing):
    requests = []
    refreshed = False

    async def handler(req):
        nonlocal refreshed
        requests.append((req.method, req.raw_path))
        if req.path.endswith("/members/refresh"):
            assert await req.json() == {}
            return web.json_response(
                {"id": "op/1", "accountId": "account/a", "kind": "metadata", "state": "queued"},
                status=202,
            )
        if "/operations/" in req.path:
            refreshed = True
            return web.json_response(
                {"id": "op/1", "accountId": "account/a", "kind": "metadata", "state": "succeeded"}
            )
        username = req.match_info["path"].rsplit("/", 1)[-1]
        if missing == "group" and not refreshed:
            return web.json_response({"error": {"code": "media_not_cached"}}, status=404)
        state = "unconfirmed" if username == missing and not refreshed else "present"
        return web.json_response(member_lookup(username, state))

    async with client(await serve(handler), poll_interval=0.001) as c:
        await c.prepare_group_mentions("room@chatroom", ["bot", "user"])
    assert sum(method == "POST" for method, _ in requests) == (missing is not None)
    assert requests[-2:] == [
        ("GET", "/v1/accounts/account%2Fa/groups/room@chatroom/members/bot"),
        ("GET", "/v1/accounts/account%2Fa/groups/room@chatroom/members/user"),
    ]
    if missing is not None:
        assert ("GET", "/v1/accounts/account%2Fa/operations/op%2F1") in requests


@pytest.mark.parametrize("outcome", ["failed", "timeout", "still_missing", "wrong_operation"])
async def test_group_member_refresh_must_finish_with_usable_members(serve, outcome):
    requests = []

    async def handler(req):
        requests.append((req.method, req.path))
        if "/members/" in req.path and req.method == "GET":
            return web.json_response(member_lookup("bot", "unconfirmed"))
        state = {
            "failed": "failed",
            "timeout": "running",
            "still_missing": "succeeded",
            "wrong_operation": "succeeded",
        }[outcome]
        return web.json_response(
            {
                "id": "op1",
                "accountId": "other" if outcome == "wrong_operation" else "account/a",
                "kind": "metadata",
                "state": state,
            }
        )

    async with client(await serve(handler), poll_interval=0.001, send_timeout=0.02) as c:
        with pytest.raises(ValueError, match="群成员"):
            await asyncio.wait_for(c.prepare_group_mentions("room@chatroom", ["bot"]), 1)
    assert sum(method == "POST" for method, _ in requests) == 1
    assert not any("/sends" in path for _, path in requests)


@pytest.mark.parametrize("code", ["media_not_cached", "mention_member_unavailable", "PRIVATE CHAT"])
async def test_http_errors_expose_only_allowlisted_native_codes(serve, code):
    async def handler(req):
        return web.json_response(
            {"error": {"code": code, "message": "fake-token PRIVATE CHAT"}}, status=404
        )

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(api.NativeAPIError) as error:
            await c.account()
    assert error.value.status == 404
    assert error.value.code == (code if code != "PRIVATE CHAT" else "request_failed")
    assert "fake-token" not in str(error.value)
    assert "PRIVATE CHAT" not in str(error.value)
