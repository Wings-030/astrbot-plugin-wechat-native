import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import module

FIXTURES = Path(__file__).with_name("fixtures")
VALID_JPEG = (FIXTURES / "baseline.jpg").read_bytes()
PROGRESSIVE_JPEG = (FIXTURES / "progressive.jpg").read_bytes()
VALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
VALID_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
VALID_WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00\x00\x00\x00\x00"


def client(url, **kwargs):
    return module("native_client").NativeClient(url + "/v1", "account/a", "fake-token", **kwargs)


@pytest.mark.parametrize("limit", [3, 4])
async def test_binary_response_is_limited_while_streaming(limit):
    api = module("native_client")
    chunks_read = []

    async def chunks(size):
        assert size <= 65536
        for chunk in (b"ab", b"cd"):
            chunks_read.append(chunk)
            yield chunk
        if limit == 3:
            pytest.fail("oversized response must be closed before reading more chunks")

    @asynccontextmanager
    async def request(*args, **kwargs):
        assert kwargs["allow_redirects"] is False
        yield SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks))

    c = client("http://localhost")
    c._session = SimpleNamespace(closed=False, request=request)
    if limit == 3:
        with pytest.raises(api.NativeAPIError, match="media_metadata_limit"):
            await c._request_bytes("GET", "/qr", limit=limit)
    else:
        assert await c._request_bytes("GET", "/qr", limit=limit) == b"abcd"
    assert chunks_read == [b"ab", b"cd"]


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


def media_asset(
    asset_id="asset-1",
    *,
    message_id="m1",
    variant="original",
    state="available",
    content_type="image/jpeg",
    data=VALID_JPEG,
    **updates,
):
    item = {
        "id": asset_id,
        "accountId": "account/a",
        "messageId": message_id,
        "kind": "image",
        "variant": variant,
        "state": state,
        "contentType": content_type,
    }
    if data is not None and state == "available" and content_type != "image/x-wxgf":
        item["byteLength"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
    item.update(updates)
    return item


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

    async with client(await serve(handler), poll_interval=0.001, send_timeout=0.05) as c:
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


@pytest.mark.parametrize("code", ["operation_failed", "auth_required", "PRIVATE CHAT"])
async def test_refresh_failure_reports_safe_operation_diagnostics(serve, code):
    operation_id = "3174b191-1933-4eee-9872-123456789012"

    async def handler(req):
        if req.method == "GET":
            return web.json_response(member_lookup("bot", "unconfirmed"))
        return web.json_response(
            {
                "id": operation_id,
                "accountId": "account/a",
                "kind": "metadata",
                "state": "failed",
                "result": {"code": code, "details": "fake-token PRIVATE CHAT"},
            }
        )

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(ValueError) as error:
            await c.prepare_group_mentions("room@chatroom", ["bot"])
    assert isinstance(error.value, api.GroupMembersUnavailable)
    assert error.value.state == "failed"
    assert error.value.code == (code if code != "PRIVATE CHAT" else "operation_failed")
    assert operation_id in str(error.value)
    assert "fake-token" not in str(error.value)
    assert "PRIVATE CHAT" not in str(error.value)


@pytest.mark.parametrize(
    "payload,expected_mime",
    [
        (VALID_JPEG, "image/jpeg"),
        (VALID_PNG, "image/png"),
        (VALID_GIF, "image/gif"),
        (VALID_WEBP, "image/webp"),
    ],
)
def test_validate_image_bytes_accepts_supported_formats(payload, expected_mime):
    api = module("native_client")
    assert api.validate_image_bytes(payload) == expected_mime


@pytest.mark.parametrize("jpeg", [VALID_JPEG, PROGRESSIVE_JPEG], ids=["baseline", "progressive"])
@pytest.mark.parametrize(
    "trailer",
    [b"", b"\x00" * 24, b"synthetic-wechat-photo-trailer"],
    ids=["none", "padding", "native-trailer"],
)
def test_validate_jpeg_allows_data_after_real_end_marker(jpeg, trailer):
    api = module("native_client")
    assert api.validate_image_bytes(jpeg + trailer) == "image/jpeg"


@pytest.mark.parametrize(
    "payload",
    [
        VALID_JPEG[:-2],
        PROGRESSIVE_JPEG[:-2],
        b"\xff\xd8\xff\xfe\x00\x06\x11\xff\xd9\x22" + VALID_JPEG[2:-2],
        b"\xff\xd8\xff\xfe\xff\xff" + VALID_JPEG[2:],
        b"\xff\xd8\xff\xfe\x00\x01\xff\xd9",
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9",
        b"\xff\xd8\xff\xda\x00\x02\x07\xff\x00\xd9\xff\xd0\x09\xff",
    ],
    ids=[
        "baseline-truncated",
        "progressive-truncated",
        "eoi-in-metadata",
        "segment-overrun",
        "invalid-segment-length",
        "missing-scan",
        "stuffed-byte-and-restart",
    ],
)
def test_validate_jpeg_rejects_truncation_and_false_end_markers(payload):
    api = module("native_client")
    with pytest.raises(api.MediaError) as error:
        api.validate_image_bytes(payload)
    assert error.value.reason == "unavailable"


@pytest.mark.parametrize(
    "payload,max_bytes,reason",
    [
        (b"", 1024, "unavailable"),
        (b"wxgf\x00\x01\x02\x03", 1024, "unsupported_format"),
        (b"   <html><body>not an image</body></html>", 1024, "unsupported_format"),
        (b"%PDF-1.5\n...", 1024, "unsupported_format"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00", 1024, "unavailable"),
        (b"\x89PNG\r\n\x1a\ntruncated", 1024, "unavailable"),
        (b"GIF89a\x01\x00\x01\x00\x00\x00\x00notrailer", 1024, "unavailable"),
        (b"RIFF\xff\x00\x00\x00WEBPVP8 \x00\x00\x00\x00", 1024, "unavailable"),
        (VALID_JPEG, 10, "too_large"),
    ],
)
def test_validate_image_bytes_rejects_invalid_or_oversized_content(payload, max_bytes, reason):
    api = module("native_client")
    with pytest.raises(api.MediaError) as error:
        api.validate_image_bytes(payload, max_bytes=max_bytes)
    assert error.value.reason == reason


async def test_fetch_image_transfers_original_when_only_thumbnail_cached(serve):
    calls = []
    transferred = False

    async def handler(req):
        nonlocal transferred
        calls.append((req.method, req.path, dict(req.query)))
        if req.path.endswith("/media") and req.method == "GET":
            items = [
                media_asset("thumb-1", variant="thumbnail", state="available", data=VALID_GIF),
                media_asset(
                    "orig-1",
                    variant="original",
                    state="available" if transferred else "pending",
                    data=VALID_PNG if transferred else None,
                    content_type="image/png",
                ),
            ]
            return web.json_response({"items": items})
        if req.path.endswith("/media-transfers") and req.method == "POST":
            body = await req.json()
            assert body == {"messageId": "m1", "quality": "original"}
            return web.json_response(
                {
                    "id": "job-1",
                    "accountId": "account/a",
                    "kind": "media",
                    "messageId": "m1",
                    "assetId": "orig-1",
                    "state": "queued",
                },
                status=202,
            )
        if "/jobs/" in req.path:
            transferred = True
            return web.json_response(
                {
                    "id": "job-1",
                    "accountId": "account/a",
                    "kind": "media",
                    "messageId": "m1",
                    "assetId": "orig-1",
                    "state": "succeeded",
                }
            )
        if req.path.endswith("/media/orig-1/content"):
            return web.Response(body=VALID_PNG, content_type="image/png")
        return web.Response(status=404)

    async with client(await serve(handler), poll_interval=0.001) as c:
        data = await c.fetch_image("m1")
    assert data == VALID_PNG
    assert sum(m == "POST" and p.endswith("/media-transfers") for m, p, _ in calls) == 1
    assert any(p.endswith("/media/orig-1/content") for _, p, _ in calls)
    assert not any(p.endswith("/media/thumb-1/content") for _, p, _ in calls)


async def test_fetch_image_keeps_standard_jpeg_with_trailer_instead_of_thumbnail(serve):
    standard = VALID_JPEG + b"synthetic-wechat-photo-trailer"
    downloads = []

    async def handler(req):
        if req.path.endswith("/messages/m1/media"):
            return web.json_response(
                {
                    "items": [
                        media_asset("standard-1", variant="standard", data=standard),
                        media_asset("thumb-1", variant="thumbnail"),
                    ]
                }
            )
        if req.path.endswith("/content"):
            asset_id = req.path.split("/")[-2]
            downloads.append(asset_id)
            return web.Response(
                body=standard if asset_id == "standard-1" else VALID_JPEG,
                content_type="image/jpeg",
            )
        return web.Response(status=404)

    async with client(await serve(handler)) as c:
        result = await c.fetch_image("m1")
    assert result == standard
    assert downloads == ["standard-1"]


async def test_fetch_image_reuses_active_job_and_falls_back_to_thumbnail_on_failure(serve):
    posts = 0

    async def handler(req):
        nonlocal posts
        if req.path.endswith("/media-transfers"):
            posts += 1
            return web.Response(status=500)
        if req.path.endswith("/media"):
            return web.json_response(
                {
                    "items": [
                        media_asset(
                            "orig-1",
                            variant="original",
                            state="running",
                            jobId="existing-job",
                            data=None,
                        ),
                        media_asset(
                            "thumb-1", variant="thumbnail", state="available", data=VALID_JPEG
                        ),
                    ]
                }
            )
        if req.path.endswith("/jobs/existing-job"):
            return web.json_response(
                {
                    "id": "existing-job",
                    "accountId": "account/a",
                    "kind": "media",
                    "messageId": "m1",
                    "assetId": "orig-1",
                    "state": "failed",
                    "error": {"code": "image_quality_unavailable"},
                }
            )
        if req.path.endswith("/media/thumb-1/content"):
            return web.Response(body=VALID_JPEG, content_type="image/jpeg")
        return web.Response(status=404)

    async with client(await serve(handler), poll_interval=0.001) as c:
        data = await c.fetch_image("m1")
    assert data == VALID_JPEG
    assert posts == 0


async def test_fetch_image_uses_jpeg_preview_for_wxgf_and_never_requests_raw_wxgf(serve):
    queries = []

    async def handler(req):
        if req.path.endswith("/media"):
            return web.json_response(
                {
                    "items": [
                        media_asset(
                            "wxgf-1",
                            variant="original",
                            state="available",
                            content_type="image/x-wxgf",
                            preview={
                                "state": "available",
                                "contentType": "image/jpeg",
                                "byteLength": len(VALID_JPEG),
                                "sha256": hashlib.sha256(VALID_JPEG).hexdigest(),
                            },
                        )
                    ]
                }
            )
        if req.path.endswith("/media/wxgf-1/content"):
            queries.append(dict(req.query))
            if req.query.get("representation") == "preview":
                return web.Response(body=VALID_JPEG, content_type="image/jpeg")
            return web.Response(body=b"wxgf-raw-bytes", content_type="image/x-wxgf")
        return web.Response(status=404)

    async with client(await serve(handler)) as c:
        data = await c.fetch_image("m1")
    assert data == VALID_JPEG
    assert queries == [{"representation": "preview"}]


async def test_media_content_stops_immediately_when_stream_exceeds_max_bytes(serve):
    chunks_sent = 0

    async def handler(req):
        nonlocal chunks_sent
        if req.path.endswith("/media"):
            return web.json_response(
                {"items": [media_asset("big-1", variant="original", state="available", data=None)]}
            )
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(req)
        for _ in range(10):
            chunks_sent += 1
            await response.write(b"\xff\xd8\xff" + b"x" * 600)
            await asyncio.sleep(0.01)
        return response

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(api.MediaError) as error:
            await c.fetch_image("m1", max_bytes=1000)
    assert error.value.reason == "too_large"
    assert chunks_sent <= 4


@pytest.mark.parametrize(
    "status,code,quoted,expected_reason",
    [
        (403, "insufficient_scope", False, "missing_scope"),
        (410, "message_recalled", False, "recalled"),
        (404, "media_not_cached", True, "expired"),
        (422, "wxgf_decode_failed", False, "unsupported_format"),
        (413, "image_size_limit", False, "too_large"),
    ],
)
async def test_fetch_image_maps_native_errors_to_domain_reasons(
    serve, status, code, quoted, expected_reason
):
    async def handler(req):
        return web.json_response({"error": {"code": code}}, status=status)

    api = module("native_client")
    async with client(await serve(handler)) as c:
        with pytest.raises(api.MediaError) as error:
            await c.fetch_image("m1", quoted=quoted)
    assert error.value.reason == expected_reason
