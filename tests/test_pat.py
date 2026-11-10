import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import VALID_JPEG


async def pat_env(serve, tmp_path, *, items=None, **options):
    pat = message(
        threadId="room@chatroom",
        type=10000,
        body=None,
        pat={"actorId": "user", "targetId": "bot"},
    )
    previous = message(
        id="previous",
        serverId="899",
        threadId=pat["threadId"],
        senderId="someone-else",
        body="周末一起去爬山",
        createdAt=(datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
    )
    rows = [pat, previous] if items is None else items
    messages = {pat["id"]: pat, previous["id"]: previous}
    reads = []

    async def custom_serve(default):
        async def handler(req):
            if req.path.endswith("/messages"):
                reads.append(dict(req.query))
                return web.json_response({"items": rows, "nextCursor": None})
            key = req.path.rsplit("/", 1)[-1]
            if "/messages/" in req.path and key in messages:
                return web.json_response(messages[key])
            return await default(req)

        return await serve(handler)

    adapter, queue, _, sent = await make_adapter(
        custom_serve, tmp_path, pat, enable_group_pat_reply=True, **options
    )
    return SimpleNamespace(
        adapter=adapter,
        queue=queue,
        pat=pat,
        previous=previous,
        rows=rows,
        messages=messages,
        reads=reads,
        sent=sent,
    )


async def deliver(env, cursor="1"):
    await env.adapter.process_native_event(native_event(env.pat, cursor))
    if env.adapter._media_tasks:
        await asyncio.gather(*list(env.adapter._media_tasks))


async def test_pat_uses_other_members_text_in_actors_session_and_deduplicates(serve, tmp_path):
    from astrbot.api.event import MessageChain

    env = await pat_env(serve, tmp_path)
    try:
        await deliver(env)
        event = env.queue.get_nowait()
        assert event.get_sender_id() == "user"
        assert event.message_obj.session_id.endswith("|user")
        assert "周末一起去爬山" in event.message_str
        assert event.message_obj.raw_message == env.pat
        await event.send(MessageChain().message("出发前记得带水！"))
        assert env.sent[0][1]["mentions"] == {"userIds": ["user"]}
        await deliver(env, "2")
        assert env.queue.empty() and len(env.reads) == 1
    finally:
        await env.adapter.terminate()


async def test_pat_does_not_respond_to_member_to_member(serve, tmp_path):
    env = await pat_env(serve, tmp_path)
    env.pat["pat"]["targetId"] = "someone-else"
    try:
        await deliver(env)
        assert env.queue.empty() and not env.reads
    finally:
        await env.adapter.terminate()


@pytest.mark.parametrize("kind", [3, 34])
async def test_pat_skips_bot_and_newer_messages_and_loads_media(serve, tmp_path, monkeypatch, kind):
    from astrbot.api.message_components import Image

    env = await pat_env(serve, tmp_path)
    env.previous.update(type=kind, body="有效文本" if kind == 1 else None)
    env.rows.insert(0, message(id="future", threadId=env.pat["threadId"], body="不能读未来消息"))
    env.rows.insert(
        2,
        message(
            id="own",
            threadId=env.pat["threadId"],
            senderId="bot",
            direction="outgoing",
            createdAt=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        ),
    )
    calls = []

    async def fetch(message_id, **kwargs):
        calls.append(message_id)
        return VALID_JPEG if kind == 3 else b"test-wav"

    monkeypatch.setattr(env.adapter.client, "fetch_image" if kind == 3 else "fetch_voice", fetch)
    try:
        await deliver(env)
        event = env.queue.get_nowait()
        assert "不能读未来消息" not in event.message_str
        if kind == 1:
            assert "有效文本" in event.message_str
        elif kind == 3:
            assert calls == ["previous"]
            assert any(isinstance(part, Image) for part in event.get_messages())
        else:
            seen = []

            async def get_text(audio_url):
                seen.append(await asyncio.to_thread(Path(audio_url).read_bytes))
                return "语音里的周末安排"

            async def provider(origin):
                return SimpleNamespace(get_text=get_text)

            plugin = SimpleNamespace(
                context=SimpleNamespace(
                    get_using_stt_provider_async=provider,
                    get_config=lambda origin: {"provider_stt_settings": {"enable": True}},
                )
            )
            req = SimpleNamespace(extra_user_content_parts=[])
            await module("main").WeChatNativePlugin.attach_pat_context(plugin, event, req)
            assert seen == [b"test-wav"]
            assert "语音里的周末安排" in req.extra_user_content_parts[0].text
        env.pat.update(id="second-pat", serverId="901")
        env.messages[env.pat["id"]] = env.pat
        await deliver(env, "2")
        assert env.queue.empty() and len(env.reads) == 1
    finally:
        await env.adapter.terminate()


async def test_pat_empty_history_still_wakes_without_quoting_system_message(serve, tmp_path):
    from astrbot.api.event import MessageChain

    env = await pat_env(serve, tmp_path, items=[], group_reply_style="quote")
    try:
        await deliver(env)
        event = env.queue.get_nowait()
        assert "拍" in event.message_str
        await event.send(MessageChain().message("我在呢"))
        assert "quote" not in env.sent[0][1]
    finally:
        await env.adapter.terminate()
