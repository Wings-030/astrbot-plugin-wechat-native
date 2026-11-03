import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import media_asset


async def make_recent_adapter(serve, tmp_path, **options):
    image = message(id="group-image", serverId="899", threadId="room@chatroom", type=3, body=None)
    request = message(
        id="image-request",
        threadId="room@chatroom",
        body="@机器人\u2005分析刚才的图片",
        mentions={"state": "known", "self": True},
    )
    messages = {image["id"]: image, request["id"]: request}
    paths = []

    async def custom_serve(default_handler):
        async def handler(req):
            paths.append(req.path)
            if req.path.endswith("/media"):
                source_id = req.path.split("/")[-2]
                return web.json_response(
                    {"items": [media_asset("asset-1", accountId="a", message_id=source_id)]}
                )
            source_id = req.path.rsplit("/", 1)[-1]
            if "/messages/" in req.path and source_id in messages:
                return web.json_response(messages[source_id])
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, sent = await make_adapter(custom_serve, tmp_path, request, **options)
    return adapter, queue, image, request, sent, paths, messages


@pytest.mark.parametrize("mode", ["mention", "all"])
@pytest.mark.parametrize("prompt", ["@机器人\u2005分析刚才的图片", "@机器人\u2005"])
async def test_group_image_then_mention_attaches_image(serve, tmp_path, mode, prompt):
    from astrbot.api.message_components import At, Image, Plain

    adapter, queue, image, request, sent, paths, _ = await make_recent_adapter(
        serve, tmp_path, group_reply_mode=mode
    )
    request["body"] = prompt
    try:
        await adapter.process_native_event(native_event(image, "1"))
        assert queue.empty()
        assert not any(path.endswith("/media") for path in paths)
        await adapter.process_native_event(native_event(request, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert [type(part) for part in event.get_messages()] == [At, Plain, Image]
        assert event.message_str == (
            "请描述这张图片的内容。" if prompt == "@机器人\u2005" else "分析刚才的图片"
        )
        assert [path for path in paths if path.endswith("/media")] == [
            "/v1/accounts/a/messages/group-image/media"
        ]
        assert event.message_obj.raw_message == request
        assert sent == []
        await adapter.process_native_event(native_event(request, "3"))
        assert queue.empty()
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("case", ["another-sender", "another-group", "expired", "no-image"])
async def test_group_recent_image_request_requires_matching_fresh_image(serve, tmp_path, case):
    adapter, queue, image, request, sent, paths, _ = await make_recent_adapter(serve, tmp_path)
    if case == "another-sender":
        image["senderId"] = "another-user"
    elif case == "another-group":
        image["threadId"] = "another@chatroom"
    elif case == "expired":
        image["createdAt"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    try:
        if case != "no-image":
            await adapter.process_native_event(native_event(image, "1"))
        await adapter.process_native_event(native_event(request, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.empty()
        assert len(sent) == 1
        assert "最近" in sent[0][1]["text"]
        assert not any(path.endswith("/media") for path in paths)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "prompt,mentioned",
    [
        ("@机器人\u2005你好", True),
        ("@机器人\u2005总结群聊", True),
        ("分析刚才的图片", False),
    ],
)
async def test_group_recent_image_keeps_other_text_rules(serve, tmp_path, prompt, mentioned):
    from astrbot.api.message_components import Image

    adapter, queue, image, request, _, paths, _ = await make_recent_adapter(
        serve, tmp_path, group_reply_mode="all", enable_group_summary=True
    )
    request["body"] = prompt
    request["mentions"]["self"] = mentioned
    try:
        await adapter.process_native_event(native_event(image, "1"))
        await adapter.process_native_event(native_event(request, "2"))
        event = queue.get_nowait()
        assert not any(isinstance(part, Image) for part in event.get_messages())
        assert not any(path.endswith("/media") for path in paths)
    finally:
        await adapter.terminate()


async def test_group_recent_image_uses_latest_image_from_same_sender(serve, tmp_path):
    adapter, queue, image, request, _, paths, messages = await make_recent_adapter(serve, tmp_path)
    latest = {**image, "id": "latest-image", "serverId": "898"}
    foreign = {**image, "id": "foreign-image", "serverId": "897", "senderId": "another-user"}
    messages.update({latest["id"]: latest, foreign["id"]: foreign})
    try:
        for cursor, source in enumerate((image, latest, foreign), 1):
            await adapter.process_native_event(native_event(source, str(cursor)))
        await adapter.process_native_event(native_event(request, "4"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.qsize() == 1
        assert [path for path in paths if path.endswith("/media")] == [
            "/v1/accounts/a/messages/latest-image/media"
        ]
    finally:
        await adapter.terminate()


async def test_group_recent_image_original_recall_before_reply_suppresses_send(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    adapter, queue, image, request, sent, _, _ = await make_recent_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(image, "1"))
        await adapter.process_native_event(native_event(request, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.qsize() == 1
        event = queue.get_nowait()
        image["recall"] = {"observedAt": "now"}
        await event.send(MessageChain([Plain("分析结果")]))
        assert event.is_stopped()
        assert sent == []
        assert adapter.state.message_status(request["id"]) == "no_reply"
    finally:
        await adapter.terminate()
