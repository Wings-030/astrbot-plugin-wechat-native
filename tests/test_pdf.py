"""PDF recognition: Native file messages -> in-memory text -> LLM request context."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import VALID_JPEG, client, media_asset

UNIQUE = "QUOKKA-ZEBRA-7319-LANTERN"


def make_pdf(pages) -> bytes:
    """A real, minimal text PDF (Helvetica); an empty string makes a text-less page."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


PDF = make_pdf([f"The secret launch code is {UNIQUE}."])


def file_asset(message_id, data, filename="report.pdf", **updates):
    asset = {
        "id": f"asset-{message_id}",
        "accountId": "a",
        "messageId": message_id,
        "kind": "file",
        "variant": "original",
        "state": "available",
        "contentType": "application/octet-stream",
        "filename": filename,
        "byteLength": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    asset.update(updates)
    return asset


def file_message(message_id="file-1", server_id="800", **updates):
    data = {
        "id": message_id,
        "serverId": server_id,
        "type": 49,
        "appMessageType": 6,
        "body": None,
    }
    data.update(updates)
    return message(**data)


def quote_message(**updates):
    data = {
        "id": "quote-1",
        "serverId": "801",
        "type": 49,
        "appMessageType": 57,
        "body": "这份文件讲了什么？",
        "quote": {
            "serverId": "800",
            "senderId": "user",
            "type": 49,
            "appMessageType": 6,
            "state": "unsupported",
            "embeddedText": "report.pdf",
            "embeddedImage": False,
        },
    }
    data.update(updates)
    return message(**data)


class Env:
    def __init__(self):
        self.messages = {}
        self.assets = {}
        self.contents = {}
        self.media_errors = {}
        self.content_errors = {}
        self.paths = []
        self.hooks = {}

    def add_file(self, msg, data=PDF, filename="report.pdf", **updates):
        self.messages[msg["id"]] = msg
        asset = file_asset(msg["id"], data, filename, **updates)
        self.assets[msg["id"]] = [asset]
        self.contents[asset["id"]] = data
        return asset

    @property
    def content_requests(self):
        return [path for path in self.paths if path.endswith("/content")]


async def make_env(serve, tmp_path, trigger, **options):
    env = Env()
    env.messages[trigger["id"]] = trigger

    async def custom_serve(default_handler):
        async def handler(req):
            env.paths.append(req.path)
            parts = req.path.split("/")
            if req.method == "GET" and req.path.endswith("/media"):
                message_id = parts[-2]
                if message_id in env.media_errors:
                    status, code = env.media_errors[message_id]
                    return web.json_response({"error": {"code": code}}, status=status)
                if message_id in env.assets:
                    return web.json_response({"items": env.assets[message_id]})
            elif req.method == "GET" and req.path.endswith("/content"):
                asset_id = parts[-2]
                hook = env.hooks.get("content")
                if hook:
                    await hook()
                if asset_id in env.content_errors:
                    status, code = env.content_errors[asset_id]
                    return web.json_response({"error": {"code": code}}, status=status)
                if asset_id in env.contents:
                    return web.Response(
                        body=env.contents[asset_id], content_type="application/octet-stream"
                    )
            elif req.method == "GET" and "/messages/" in req.path:
                if parts[-1] in env.messages:
                    return web.json_response(env.messages[parts[-1]])
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, sent = await make_adapter(custom_serve, tmp_path, trigger, **options)
    env.adapter, env.queue, env.sent = adapter, queue, sent
    return env


async def deliver(env, msg, cursor="1"):
    await env.adapter.process_native_event(native_event(msg, cursor))
    await asyncio.gather(*list(env.adapter._media_tasks))


async def settle_original(env, original, cursor="1"):
    """Deliver the quoted file; a private file is itself a request, so discard that turn."""
    await deliver(env, original, cursor)
    while not env.queue.empty():
        env.queue.get_nowait()
    env.paths.clear()


async def llm_context(event):
    """The user turn AstrBot would hand to a provider for this event."""
    from astrbot.api.provider import ProviderRequest

    plugin = module("main").WeChatNativePlugin
    req = ProviderRequest(prompt=event.message_str)
    await plugin.attach_pdf_text(None, event, req)
    return req, await req.assemble_context()


def context_text(context) -> str:
    content = context["content"]
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))


# ---------------------------------------------------------------- extraction


def test_extract_pdf_text_reads_unique_text_per_page():
    text = module("native_client").extract_pdf_text(make_pdf([f"first {UNIQUE}", "second page"]))
    assert f"[第 1 页]\nfirst {UNIQUE}" in text
    assert "[第 2 页]\nsecond page" in text


def test_extract_pdf_text_bounds_pages_and_characters():
    client_module = module("native_client")
    many = make_pdf([f"page-{i}" for i in range(1, 8)])
    text = client_module.extract_pdf_text(many, max_pages=3)
    assert "page-3" in text and "page-4" not in text
    assert "共 7 页" in text
    long = make_pdf(["x" * 3000])
    text = client_module.extract_pdf_text(long, max_chars=500)
    assert len(text) < 700 and "已截取" in text


@pytest.mark.parametrize(
    "data,reason",
    [
        (make_pdf([""]), "empty_pdf_text"),
        (b"%PDF-1.4\nnot really a pdf at all", "unsupported_format"),
        (b"MZ\x90\x00 not a pdf", "unsupported_format"),
        (b"", "unavailable"),
    ],
)
def test_extract_pdf_text_rejects_unreadable_files(data, reason):
    client_module = module("native_client")
    with pytest.raises(client_module.MediaError) as error:
        client_module.extract_pdf_text(data)
    assert error.value.reason == reason
    assert error.value.media_kind == "pdf"


def test_extract_pdf_text_rejects_encrypted_and_oversized_files():
    import io

    from pypdf import PdfReader, PdfWriter

    client_module = module("native_client")
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(PDF)))
    writer.encrypt("secret", algorithm="AES-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(client_module.MediaError) as error:
        client_module.extract_pdf_text(buffer.getvalue())
    assert error.value.reason == "encrypted_pdf"
    with pytest.raises(client_module.MediaError) as error:
        client_module.extract_pdf_text(PDF, max_bytes=len(PDF) - 1)
    assert error.value.reason == "too_large"


# ------------------------------------------------------------ Native client


async def test_fetch_pdf_downloads_verified_bytes_and_name(serve):
    asset = file_asset("m1", PDF)
    paths = []

    async def handler(req):
        paths.append(req.path)
        if req.path.endswith("/media"):
            return web.json_response({"items": [asset]})
        return web.Response(body=PDF, content_type="application/octet-stream")

    async with client(await serve(handler)) as c:
        asset["accountId"] = "account/a"
        data, name = await c.fetch_pdf("m1")
    assert (data, name) == (PDF, "report.pdf")
    assert paths == [
        "/v1/accounts/account/a/messages/m1/media",
        "/v1/accounts/account/a/media/asset-m1/content",
    ]


async def test_fetch_pdf_transfers_pending_attachment_then_downloads(serve):
    state = {"transferred": False}
    calls = []

    async def handler(req):
        calls.append((req.method, req.path.rsplit("/", 1)[-1]))
        if req.path.endswith("/media-transfers"):
            assert await req.json() == {"messageId": "m1", "quality": "original"}
            state["transferred"] = True
            return web.json_response(
                {"id": "job-1", "accountId": "account/a", "messageId": "m1", "state": "queued"}
            )
        if req.path.endswith("/jobs/job-1"):
            return web.json_response(
                {
                    "id": "job-1",
                    "accountId": "account/a",
                    "kind": "download",
                    "messageId": "m1",
                    "state": "succeeded",
                }
            )
        if req.path.endswith("/media"):
            asset = file_asset("m1", PDF, accountId="account/a")
            if not state["transferred"]:
                asset.update(state="pending", byteLength=None, sha256=None)
                asset["advertisedByteLength"] = len(PDF)
            return web.json_response({"items": [asset]})
        return web.Response(body=PDF, content_type="application/octet-stream")

    async with client(await serve(handler), poll_interval=0.01) as c:
        data, name = await c.fetch_pdf("m1")
    assert data == PDF and name == "report.pdf"
    assert ("POST", "media-transfers") in calls


@pytest.mark.parametrize(
    "filename,reason",
    [("notes.docx", "not_pdf"), (None, "not_pdf"), ("archive.zip", "not_pdf")],
)
async def test_fetch_pdf_ignores_non_pdf_without_downloading(serve, filename, reason):
    paths = []

    async def handler(req):
        paths.append(req.path)
        asset = file_asset("m1", b"data", filename, accountId="account/a")
        return web.json_response({"items": [asset]})

    async with client(await serve(handler)) as c:
        with pytest.raises(module("native_client").MediaError) as error:
            await c.fetch_pdf("m1")
    assert error.value.reason == reason
    assert not any(path.endswith("/content") for path in paths)


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"byteLength": 99 * 1024 * 1024}, "too_large"),
        (
            {"state": "pending", "byteLength": None, "advertisedByteLength": 99 * 1024 * 1024},
            "too_large",
        ),
        ({"state": "expired"}, "expired"),
        ({"state": "unsupported"}, "unsupported_format"),
        ({"state": "recalled"}, "recalled"),
        ({"sha256": "0" * 64}, "unavailable"),
    ],
)
async def test_fetch_pdf_maps_asset_states_without_unbounded_reads(serve, updates, reason):
    async def handler(req):
        if req.path.endswith("/media"):
            asset = file_asset("m1", PDF, accountId="account/a", **updates)
            return web.json_response({"items": [asset]})
        return web.Response(body=PDF, content_type="application/octet-stream")

    async with client(await serve(handler)) as c:
        with pytest.raises(module("native_client").MediaError) as error:
            await c.fetch_pdf("m1")
    assert error.value.reason == reason


async def test_fetch_pdf_enforces_streaming_size_limit(serve):
    big = b"%PDF-1.4\n" + b"0" * 2048

    async def handler(req):
        if req.path.endswith("/media"):
            asset = file_asset("m1", big, accountId="account/a")
            asset.pop("byteLength"), asset.pop("sha256")
            return web.json_response({"items": [asset]})
        return web.Response(body=big, content_type="application/octet-stream")

    async with client(await serve(handler)) as c:
        with pytest.raises(module("native_client").MediaError) as error:
            await c.fetch_pdf("m1", max_bytes=1024)
    assert error.value.reason == "too_large"


@pytest.mark.parametrize(
    "status,code,reason",
    [(403, "insufficient_scope", "missing_scope"), (410, "message_recalled", "recalled")],
)
async def test_fetch_pdf_maps_native_errors(serve, status, code, reason):
    async def handler(req):
        return web.json_response({"error": {"code": code}}, status=status)

    async with client(await serve(handler)) as c:
        with pytest.raises(module("native_client").MediaError) as error:
            await c.fetch_pdf("m1")
    assert error.value.reason == reason
    assert error.value.media_kind == "pdf"


async def test_fetch_pdf_obeys_media_deadline(serve):
    async def handler(req):
        if req.path.endswith("/media"):
            return web.json_response({"items": [file_asset("m1", PDF, accountId="account/a")]})
        await asyncio.sleep(5)
        return web.Response(body=PDF)

    async with client(await serve(handler), media_timeout=0.1) as c:
        with pytest.raises(module("native_client").MediaError) as error:
            await asyncio.wait_for(c.fetch_pdf("m1"), 2)
    assert error.value.reason == "unavailable"


# ----------------------------------------------------------- private chats


async def test_private_pdf_reaches_the_llm_request_with_default_prompt(serve, tmp_path):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg)
    try:
        await deliver(env, msg)
        event = env.queue.get_nowait()
        assert event.message_str == "请阅读并总结这份 PDF 文件的主要内容。"
        assert env.sent == []
        req, context = await llm_context(event)
        # The prompt stays the user's request; the file text rides along as reference.
        assert req.prompt == event.message_str
        assert UNIQUE in context_text(context)
        assert "report.pdf" in context_text(context)
        await env.adapter.process_native_event(native_event(msg, "2"))
        assert env.queue.empty()
        assert len(env.content_requests) == 1
    finally:
        await env.adapter.terminate()


async def test_llm_hook_ignores_other_events_and_is_idempotent(serve, tmp_path):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg)
    try:
        await deliver(env, msg)
        event = env.queue.get_nowait()
        req, _ = await llm_context(event)
        await module("main").WeChatNativePlugin.attach_pdf_text(None, event, req)
        assert len(req.extra_user_content_parts) == 1
        event.set_extra("wechat_native_pdf", None)
        from astrbot.api.provider import ProviderRequest

        plain = ProviderRequest(prompt="你好")
        await module("main").WeChatNativePlugin.attach_pdf_text(None, event, plain)
        assert plain.extra_user_content_parts == []
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("filename", ["notes.docx", "photo.zip", None])
async def test_private_non_pdf_file_is_ignored_silently(serve, tmp_path, filename):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg, b"PK\x03\x04data", filename)
    try:
        await deliver(env, msg)
        assert env.queue.empty() and env.sent == []
        assert env.content_requests == []
        assert env.adapter.state.message_status("file-1") == "no_reply"
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "updates,options",
    [
        ({}, {"enable_media": False}),
        ({}, {"enable_private_reply": False}),
        ({}, {"allowed_users": ["someone-else"]}),
        ({"direction": "outgoing"}, {}),
        ({"senderId": "bot"}, {}),
        ({"recall": {"observedAt": "now"}}, {}),
        ({"appMessageType": 74}, {}),
        ({"createdAt": (datetime.now(UTC) - timedelta(days=1)).isoformat()}, {}),
    ],
)
async def test_pdf_obeys_image_switches_and_policy(serve, tmp_path, updates, options):
    msg = file_message(**updates)
    env = await make_env(serve, tmp_path, msg, **options)
    env.add_file(msg)
    try:
        await deliver(env, msg)
        assert env.queue.empty() and env.sent == []
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "build,expected",
    [
        ("too_large", "太大"),
        ("download_failed", "无法读取"),
        ("scanned", "未能从该 PDF 中提取到文字"),
        ("corrupt", "已损坏"),
        ("missing_scope", "未正确配置"),
    ],
)
async def test_private_pdf_failures_reply_once_without_model_dispatch(
    serve, tmp_path, build, expected
):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    data = {
        "scanned": make_pdf([""]),
        "corrupt": b"%PDF-1.4\ngarbage garbage garbage",
    }.get(build, PDF)
    extra = {"byteLength": 99 * 1024 * 1024} if build == "too_large" else {}
    if build in {"scanned", "corrupt"}:
        extra = {}
    asset = env.add_file(msg, data, **extra)
    if build == "download_failed":
        env.content_errors[asset["id"]] = (500, "unavailable")
    if build == "missing_scope":
        env.content_errors[asset["id"]] = (403, "insufficient_scope")
    try:
        await deliver(env, msg)
        assert env.queue.empty()
        assert [payload["text"] for _, payload in env.sent] == [
            next(text for text in {payload["text"] for _, payload in env.sent} if expected in text)
        ]
        assert env.adapter.state.message_status("file-1") == "replied"
    finally:
        await env.adapter.terminate()


async def test_private_pdf_recalled_before_download_is_silent(serve, tmp_path):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg, state="recalled")
    try:
        await deliver(env, msg)
        assert env.queue.empty() and env.sent == []
        assert env.adapter.state.message_status("file-1") == "recalled"
    finally:
        await env.adapter.terminate()


async def test_private_pdf_recalled_during_download_is_never_delivered(serve, tmp_path):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg)

    async def recall_now():
        env.adapter.state.recall("peer", "800", "file-1")

    env.hooks["content"] = recall_now
    try:
        await deliver(env, msg)
        assert env.queue.empty() and env.sent == []
        assert env.adapter.state.message_status("file-1") == "recalled"
    finally:
        await env.adapter.terminate()


async def test_private_pdf_obeys_media_deadline(serve, tmp_path):
    msg = file_message()
    env = await make_env(serve, tmp_path, msg)
    env.add_file(msg)
    release = asyncio.Event()

    async def stall():
        await release.wait()

    env.hooks["content"] = stall
    env.adapter.client.media_timeout = 0.1
    try:
        await asyncio.wait_for(deliver(env, msg), 2)
        assert env.queue.empty()
        assert len(env.sent) == 1 and "无法读取" in env.sent[0][1]["text"]
    finally:
        release.set()
        await env.adapter.terminate()


# ------------------------------------------------------------------ quotes


@pytest.mark.parametrize("thread,mentioned", [("peer", False), ("room@chatroom", True)])
async def test_quoted_pdf_resolves_same_chat_original(serve, tmp_path, thread, mentioned):
    from astrbot.api.message_components import Plain, Reply

    original = file_message(threadId=thread)
    quote = quote_message(
        threadId=thread,
        mentions={"state": "known", "self": mentioned},
        body=("@机器人\u2005" if mentioned else "") + "这份文件讲了什么？",
    )
    env = await make_env(serve, tmp_path, quote)
    env.add_file(original)
    try:
        # The original passes through the adapter first: a group file is only recorded,
        # a private file is itself a direct PDF request.
        await deliver(env, original, "1")
        if thread == "peer":
            env.queue.get_nowait()
        else:
            assert env.queue.empty() and env.content_requests == []
        seen = len(env.paths)
        await deliver(env, quote, "2")
        event = env.queue.get_nowait()
        assert event.message_str == "这份文件讲了什么？"
        reply = next(part for part in event.get_messages() if isinstance(part, Reply))
        assert reply.message_str == "[PDF 文件] report.pdf"
        assert any(isinstance(part, Plain) for part in event.get_messages())
        _, context = await llm_context(event)
        assert UNIQUE in context_text(context)
        # The trigger's own media is never requested; only the original file is.
        assert [p for p in env.paths[seen:] if p.endswith("/media")] == [
            "/v1/accounts/a/messages/file-1/media"
        ]
        assert env.sent == []
    finally:
        await env.adapter.terminate()


async def test_group_quoted_pdf_requires_a_genuine_mention(serve, tmp_path):
    original = file_message(threadId="room@chatroom")
    quote = quote_message(threadId="room@chatroom", mentions={"state": "known", "self": False})
    env = await make_env(serve, tmp_path, quote, group_reply_mode="all")
    env.add_file(original)
    try:
        await deliver(env, original, "1")
        await deliver(env, quote, "2")
        assert env.queue.empty() and env.sent == []
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


async def test_quote_of_non_pdf_file_keeps_ordinary_quote_handling(serve, tmp_path):
    from astrbot.api.message_components import Reply

    original = file_message()
    quote = quote_message()
    quote["quote"]["embeddedText"] = "notes.docx"
    env = await make_env(serve, tmp_path, quote)
    env.add_file(original, b"PK", "notes.docx")
    try:
        await deliver(env, original, "1")
        await deliver(env, quote, "2")
        event = env.queue.get_nowait()
        assert event.message_str == "这份文件讲了什么？"
        assert event.get_extra("wechat_native_pdf") is None
        reply = next(part for part in event.get_messages() if isinstance(part, Reply))
        assert "不支持" in reply.message_str
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


async def test_quoted_pdf_whose_original_is_unknown_asks_to_resend(serve, tmp_path):
    quote = quote_message()
    env = await make_env(serve, tmp_path, quote)
    try:
        await deliver(env, quote)
        assert env.queue.empty()
        assert len(env.sent) == 1 and "已失效" in env.sent[0][1]["text"]
        assert not any("/messages/800" in path for path in env.paths)
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("threadId", "other-peer"),
        ("accountId", "other"),
        ("senderId", "someone-else"),
        ("serverId", "other-server"),
        ("appMessageType", 5),
        ("type", 3),
    ],
)
async def test_quoted_pdf_original_identity_must_match(serve, tmp_path, field, value):
    original = file_message()
    quote = quote_message()
    env = await make_env(serve, tmp_path, quote)
    env.add_file(original)
    try:
        await settle_original(env, original)
        env.messages["file-1"] = {**original, field: value}
        await deliver(env, quote, "2")
        assert env.queue.empty()
        assert env.content_requests == []
        assert len(env.sent) == 1
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("when", ["before", "after_dispatch", "quote_state", "recall_event"])
async def test_quoted_pdf_recall_suppresses_the_reply(serve, tmp_path, when):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    original = file_message()
    quote = quote_message()
    if when == "quote_state":
        quote["quote"]["state"] = "recalled"
    env = await make_env(serve, tmp_path, quote)
    env.add_file(original)
    if when == "before":
        env.messages["file-1"] = {**original, "recall": {"observedAt": "now"}}
    try:
        await settle_original(env, original)
        await deliver(env, quote, "2")
        if when in {"after_dispatch", "recall_event"}:
            event = env.queue.get_nowait()
            if when == "after_dispatch":
                env.messages["file-1"] = {**original, "recall": {"observedAt": "now"}}
            else:
                await env.adapter.process_native_event(
                    {
                        "cursor": "3",
                        "kind": "message.recalled",
                        "data": {"threadId": "peer", "serverId": "800", "messageId": "file-1"},
                    }
                )
            await event.send(MessageChain([Plain("根据文件生成的回答")]))
            assert event.is_stopped()
        assert env.queue.empty()
        assert env.sent == []
        assert env.adapter.state.message_status("quote-1") == "no_reply"
    finally:
        await env.adapter.terminate()


# ------------------------------------------------------------ group recent


def group_request(body="@机器人\u2005读取文档", **updates):
    return message(
        id="request-1",
        serverId="802",
        threadId="room@chatroom",
        body=body,
        mentions={"state": "known", "self": True},
        **updates,
    )


@pytest.mark.parametrize("mode", ["mention", "all"])
@pytest.mark.parametrize(
    "body,prompt",
    [
        ("@机器人\u2005读取文档", "读取文档"),
        ("@机器人\u2005读取PDF", "读取PDF"),
        ("@机器人\u2005读取pdf", "读取pdf"),
        ("@机器人\u2005读取附件", "读取附件"),
        ("@机器人\u2005读取文档 这份文件的结论是什么", "读取文档 这份文件的结论是什么"),
    ],
)
async def test_group_pdf_then_mention_attaches_text(serve, tmp_path, mode, body, prompt):
    file = file_message(threadId="room@chatroom")
    request = group_request(body)
    env = await make_env(serve, tmp_path, request, group_reply_mode=mode)
    env.add_file(file)
    try:
        await deliver(env, file, "1")
        assert env.queue.empty() and env.content_requests == []
        await deliver(env, request, "2")
        event = env.queue.get_nowait()
        assert event.message_str == prompt
        assert event.message_obj.raw_message == request
        _, context = await llm_context(event)
        assert UNIQUE in context_text(context)
        assert env.sent == []
        await env.adapter.process_native_event(native_event(request, "3"))
        assert env.queue.empty() and len(env.content_requests) == 1
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("case", ["another-sender", "another-group", "expired", "no-file"])
async def test_group_recent_pdf_needs_matching_fresh_file(serve, tmp_path, case):
    file = file_message(threadId="room@chatroom")
    if case == "another-sender":
        file["senderId"] = "another-user"
    elif case == "another-group":
        file["threadId"] = "another@chatroom"
    elif case == "expired":
        file["createdAt"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    request = group_request()
    env = await make_env(serve, tmp_path, request)
    env.add_file(file)
    try:
        if case != "no-file":
            await deliver(env, file, "1")
        await deliver(env, request, "2")
        assert env.queue.empty()
        assert len(env.sent) == 1 and "最近" in env.sent[0][1]["text"]
        assert "PDF" in env.sent[0][1]["text"]
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


async def test_group_pdf_request_without_mention_is_ignored(serve, tmp_path):
    file = file_message(threadId="room@chatroom")
    request = group_request("读取文档")
    request["mentions"]["self"] = False
    env = await make_env(serve, tmp_path, request, group_reply_mode="all")
    env.add_file(file)
    try:
        await deliver(env, file, "1")
        await deliver(env, request, "2")
        event = env.queue.get_nowait()  # ordinary "all" mode text, no file attached
        assert event.get_extra("wechat_native_pdf") is None
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


async def test_group_generic_file_words_do_not_hijack_chat_without_a_recent_file(serve, tmp_path):
    request = group_request("@机器人\u2005帮我写一份工作文档")
    env = await make_env(serve, tmp_path, request)
    try:
        await deliver(env, request)
        event = env.queue.get_nowait()
        assert event.get_extra("wechat_native_pdf") is None
        assert env.sent == [] and env.content_requests == []
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("recent", [False, True])
@pytest.mark.parametrize(
    "prompt",
    [
        "",
        "分析刚才的图片",
        "看看这张图",
        "PDF是什么格式？",
        "帮我写一份工作文档",
        "这份文件的结论是什么",
        "听听刚才的语音",
        "转文字",
        "说了什么",
        "如何使用读取附件？",
        "读取图片的功能怎么配置？",
        "读取文档功能",
        "读取语音识别配置",
        "读取附",
    ],
)
async def test_group_media_words_and_bare_mention_stay_ordinary_chat(
    serve, tmp_path, recent, prompt
):
    request = group_request("@机器人\u2005" + prompt)
    env = await make_env(serve, tmp_path, request)
    sources = [
        message(id="image-1", serverId="799", threadId="room@chatroom", type=3, body=None),
        message(id="voice-1", serverId="798", threadId="room@chatroom", type=34, body=None),
        file_message(threadId="room@chatroom"),
    ]
    env.messages.update({source["id"]: source for source in sources})
    try:
        if recent:
            for cursor, source in enumerate(sources, 1):
                await deliver(env, source, str(cursor))
            assert env.queue.empty()
        await deliver(env, request, "4")
        event = env.queue.get_nowait()
        assert event.message_str == prompt
        assert event.get_extra("wechat_native_media_kind") is None
        assert env.sent == []
        assert not any(path.endswith(("/media", "/content")) for path in env.paths)
    finally:
        await env.adapter.terminate()


async def test_group_read_attachment_without_recent_media_asks_to_resend(serve, tmp_path):
    request = group_request("@机器人\u2005读取附件")
    env = await make_env(serve, tmp_path, request)
    try:
        await deliver(env, request)
        assert env.queue.empty()
        assert len(env.sent) == 1
        assert "未找到" in env.sent[0][1]["text"] and "附件" in env.sent[0][1]["text"]
        assert not any(path.endswith(("/media", "/content")) for path in env.paths)
    finally:
        await env.adapter.terminate()


async def test_group_recent_non_pdf_file_falls_back_to_ordinary_chat(serve, tmp_path):
    file = file_message(threadId="room@chatroom")
    request = group_request("@机器人\u2005读取文档")
    env = await make_env(serve, tmp_path, request)
    env.add_file(file, b"PK", "notes.docx")
    try:
        await deliver(env, file, "1")
        await deliver(env, request, "2")
        event = env.queue.get_nowait()
        assert event.message_str == "读取文档"
        assert event.get_extra("wechat_native_pdf") is None
        assert env.sent == [] and env.content_requests == []
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("command,kind", [("读取附件", "pdf"), ("读取图片", "image")])
async def test_group_recent_media_picks_the_requested_or_newest_kind(
    serve, tmp_path, command, kind
):
    image = message(id="image-1", serverId="799", threadId="room@chatroom", type=3, body=None)
    file = file_message(threadId="room@chatroom")
    file["createdAt"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    request = group_request("@机器人\u2005" + command)
    env = await make_env(serve, tmp_path, request)
    env.messages["image-1"] = image
    asset = media_asset("image-asset", accountId="a", message_id="image-1")
    env.assets["image-1"] = [asset]
    env.contents[asset["id"]] = VALID_JPEG
    env.add_file(file)
    try:
        await deliver(env, image, "1")
        await deliver(env, file, "2")
        await deliver(env, request, "3")
        event = env.queue.get_nowait()
        assert event.get_extra("wechat_native_media_kind") == kind
        assert [path for path in env.paths if path.endswith("/media")] == [
            f"/v1/accounts/a/messages/{'file-1' if kind == 'pdf' else 'image-1'}/media"
        ]
        if kind == "pdf":
            assert UNIQUE in event.get_extra("wechat_native_pdf")["text"]
    finally:
        await env.adapter.terminate()


async def test_group_recent_pdf_recall_forgets_the_file(serve, tmp_path):
    file = file_message(threadId="room@chatroom")
    request = group_request()
    env = await make_env(serve, tmp_path, request)
    env.add_file(file)
    try:
        await deliver(env, file, "1")
        await env.adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"threadId": "room@chatroom", "serverId": "800", "messageId": "file-1"},
            }
        )
        await deliver(env, request, "3")
        assert env.queue.empty()
        assert len(env.sent) == 1 and "最近" in env.sent[0][1]["text"]
        assert env.content_requests == []
    finally:
        await env.adapter.terminate()


async def test_group_direct_pdf_alone_never_downloads(serve, tmp_path):
    file = file_message(threadId="room@chatroom")
    env = await make_env(serve, tmp_path, file)
    env.add_file(file)
    try:
        await deliver(env, file)
        assert env.queue.empty() and env.sent == []
        assert not any(path.endswith(("/media", "/content")) for path in env.paths)
    finally:
        await env.adapter.terminate()
