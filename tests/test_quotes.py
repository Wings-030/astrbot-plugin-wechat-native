import pytest
from aiohttp import web
from test_adapter import (
    make_adapter,
    message,
    native_event,
    no_telemetry,  # noqa: F401
)
from test_native_client import client, receipt


@pytest.mark.parametrize("thread", ["peer", "room@chatroom"])
async def test_reply_component_sends_native_quote_with_group_auto_mention(serve, tmp_path, thread):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(
        serve, tmp_path, message(threadId=thread, mentions={"state": "known", "self": True})
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain([Reply(id="m1", message_str="不可信正文"), Plain("同意")]))
        expected = {
            "threadId": thread,
            "text": "同意",
            "quote": {"messageId": "m1", "serverId": "900"},
        }
        if thread.endswith("@chatroom"):
            expected["mentions"] = {"userIds": ["user"]}
        assert sent[0][1] == expected
        assert adapter.state.message_status("m1") == "replied"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "recipient,mentions",
    [
        ("user", {"userIds": ["user"]}),
        ("other", {"userIds": ["other"]}),
        ("all", {"all": True}),
    ],
)
async def test_quote_preserves_explicit_group_mentions(serve, tmp_path, recipient, mentions):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Plain, Reply

    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        message(threadId="room@chatroom", mentions={"state": "known", "self": True}),
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        await event.send(MessageChain([Reply(id="m1"), At(qq=recipient), Plain("同意")]))
        assert sent[0][1]["mentions"] == mentions
        assert sent[0][1]["quote"] == {"messageId": "m1", "serverId": "900"}
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("case", ["multiple", "empty_id", "too_long"])
async def test_invalid_quote_chain_is_rejected_before_submission(serve, tmp_path, case):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    parts = {
        "multiple": [Reply(id="m1"), Reply(id="m2"), Plain("回复")],
        "empty_id": [Reply(id=""), Plain("回复")],
        "too_long": [Reply(id="m1"), Plain("中" * 5462)],
    }[case]
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        message(threadId="room@chatroom", mentions={"state": "known", "self": True}),
    )
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        with pytest.raises(ValueError):
            await event.send(MessageChain(parts))
        assert sent == []
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "changes",
    [
        {"accountId": "foreign"},
        {"threadId": "other"},
        {"id": "wrong"},
        {"serverId": None},
        {"serverId": 900},
        {"recall": {"observedAt": "now"}},
    ],
)
async def test_quote_target_must_be_available_in_same_account_and_chat(serve, tmp_path, changes):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, _, _, sent = await make_adapter(serve, tmp_path)
    target = message(**changes)

    async def read(message_id):
        return target

    adapter.client.message = read
    try:
        with pytest.raises(ValueError):
            await adapter.send_chain("peer", MessageChain([Reply(id="m1"), Plain("回复")]))
        assert sent == []
        assert adapter.state.db.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("mentions", [None, {"userIds": ["other"]}, {"all": True}])
async def test_native_quote_preserves_server_id_and_recovers_original_receipt(serve, mentions):
    requests = []

    async def handler(req):
        requests.append(req.method)
        if req.method == "POST":
            expected = {
                "threadId": "room@chatroom",
                "text": "回复",
                "quote": {"messageId": "opaque/id", "serverId": "18446744073709551615"},
            }
            if mentions:
                expected["mentions"] = mentions
            assert await req.json() == expected
            return web.json_response({"error": {"code": "timeout"}}, status=503)
        assert req.query["key"] == "original-quote-key"
        return web.json_response(receipt())

    async with client(await serve(handler)) as c:
        result = await c.send_text(
            "room@chatroom",
            "回复",
            "original-quote-key",
            mentions=mentions,
            quote={
                "messageId": "opaque/id",
                "serverId": "18446744073709551615",
            },
        )
    assert result["state"] == "accepted"
    assert requests == ["POST", "GET"]


async def test_source_recall_during_other_quote_read_still_prevents_reply(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    original = adapter.client.message

    async def read(message_id):
        if message_id == "m2":
            adapter.state.recall("peer", "900", "m1")
            return message(id="m2", serverId="901")
        return await original(message_id)

    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        adapter.client.message = read
        await event.send(MessageChain([Reply(id="m2"), Plain("回复")]))
        assert sent == []
        assert adapter.state.message_status("m1") == "recalled"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("embedded", "引用原文"),
        ("recalled", "撤回"),
        ("missing", "不可用"),
        ("unsupported", "不支持"),
        ("unknown", "不可用"),
    ],
)
async def test_incoming_quote_exposes_only_available_embedded_text(
    serve, tmp_path, state, expected
):
    from astrbot.api.message_components import Reply

    msg = message(
        type=49,
        appMessageType=57,
        body="你的看法呢？",
        quote={
            "serverId": "18446744073709551615",
            "type": 1,
            "state": state,
            "senderId": "author",
            "embeddedText": "引用原文",
        },
    )
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        assert not queue.empty()
        event = queue.get_nowait()
        quote = next(
            component for component in event.get_messages() if isinstance(component, Reply)
        )
        assert expected in quote.message_str
        if state != "embedded":
            assert "引用原文" not in quote.message_str
        assert event.message_str == "你的看法呢？"
        assert event.message_obj.raw_message["quote"]["serverId"] == "18446744073709551615"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "kind,subtype,mentioned,expected",
    [
        (49, 57, True, True),
        (49, 57, False, False),
        (49, 5, True, False),
    ],
)
async def test_incoming_quotes_obey_group_trigger_rules(
    serve,
    tmp_path,
    kind,
    subtype,
    mentioned,
    expected,
):
    msg = message(
        threadId="room@chatroom",
        type=kind,
        appMessageType=subtype,
        mentions={"state": "known", "self": mentioned},
    )
    adapter, queue, _, _ = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        assert (not queue.empty()) is expected
    finally:
        await adapter.terminate()


async def test_quote_can_target_another_saved_message(serve, tmp_path):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain, Reply

    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    original = adapter.client.message

    async def read(message_id):
        if message_id == "m2":
            return message(id="m2", serverId="18446744073709551615")
        return await original(message_id)

    try:
        await adapter.process_native_event(native_event(msg))
        adapter.client.message = read
        await queue.get_nowait().send(MessageChain([Reply(id="m2"), Plain("回复旧消息")]))
        assert sent[0][1]["quote"] == {
            "messageId": "m2",
            "serverId": "18446744073709551615",
        }
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("use_recall_event", [True, False])
async def test_embedded_reference_recall_cancels_reply(serve, tmp_path, use_recall_event):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    msg = message(
        type=49,
        appMessageType=57,
        body="解释一下",
        quote={
            "serverId": "899",
            "type": 1,
            "state": "embedded",
            "senderId": "author",
            "embeddedText": "随后撤回的敏感文本",
        },
    )
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        if use_recall_event:
            await adapter.process_native_event(
                {
                    "cursor": "2",
                    "kind": "message.recalled",
                    "data": {"threadId": "peer", "serverId": "899", "messageId": "original"},
                }
            )
            assert event.is_stopped()
        else:
            msg["quote"] = {"serverId": "899", "type": 1, "state": "recalled"}
        await event.send(MessageChain([Plain("根据引用文本生成的旧回答")]))
        assert sent == []
        assert adapter.state.message_status("m1") == "no_reply"
    finally:
        await adapter.terminate()


async def test_private_quoted_image_resolves_via_current_message_id(serve, tmp_path):
    import asyncio

    from astrbot.api.message_components import Image, Plain, Reply

    media_paths = []
    msg = message(
        id="quote-msg-1",
        type=49,
        appMessageType=57,
        body="帮我看看这张图",
        quote={
            "serverId": "original-server-id-899",
            "type": 3,
            "state": "embedded",
            "senderId": "author",
            "embeddedImage": True,
            "embeddedText": "",
        },
    )

    async def custom_serve(default_handler):
        async def handler(req):
            if req.path.endswith("/media"):
                media_paths.append(req.path)
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, _ = await make_adapter(custom_serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await asyncio.wait_for(queue.get(), 1)
        assert media_paths == ["/v1/accounts/a/messages/quote-msg-1/media"]
        assert event.message_str == "帮我看看这张图"
        components = event.get_messages()
        assert [type(c) for c in components] == [Reply, Plain, Image]
        assert components[0].message_str == "[图片]"
        assert "不支持" not in components[0].message_str
        assert not any(isinstance(c, Image) for c in components[0].chain)
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "mode,mentioned,body,expected_prompt",
    [
        ("mention", True, "@机器人\u2005帮我分析这张图", "帮我分析这张图"),
        ("mention", True, "@机器人\u2005", "请描述这张图片的内容。"),
        ("all", True, "@机器人\u2005看看", "看看"),
        ("mention", False, "帮我分析这张图", None),
        ("all", False, "帮我分析这张图", None),
        ("disabled", True, "@机器人\u2005帮我分析这张图", None),
    ],
)
async def test_group_quoted_image_requires_genuine_mention_even_in_all_mode(
    serve, tmp_path, mode, mentioned, body, expected_prompt
):
    import asyncio

    from astrbot.api.message_components import At, Image, Plain, Reply

    media_requests = []
    msg = message(
        threadId="room@chatroom",
        type=49,
        appMessageType=57,
        body=body,
        mentions={"state": "known", "self": mentioned},
        quote={
            "serverId": "899",
            "type": 3,
            "state": "embedded",
            "senderId": "author",
            "embeddedImage": True,
        },
    )

    async def custom_serve(default_handler):
        async def handler(req):
            if "/media" in req.path:
                media_requests.append(req.path)
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, _ = await make_adapter(custom_serve, tmp_path, msg, group_reply_mode=mode)
    try:
        await adapter.process_native_event(native_event(msg))
        if expected_prompt is None:
            assert not adapter._media_tasks
            assert queue.empty()
            assert media_requests == []
        else:
            event = await asyncio.wait_for(queue.get(), 1)
            assert event.message_str == expected_prompt
            assert [type(c) for c in event.get_messages()] == [At, Reply, Plain, Image]
            assert len(media_requests) == 2
    finally:
        await adapter.terminate()


async def test_group_summary_command_on_quoted_image_skips_media_download(serve, tmp_path):
    media_requests = []
    msg = message(
        threadId="room@chatroom",
        type=49,
        appMessageType=57,
        body="@机器人\u2005总结群聊",
        mentions={"state": "known", "self": True},
        quote={
            "serverId": "899",
            "type": 3,
            "state": "embedded",
            "senderId": "author",
            "embeddedImage": True,
        },
    )

    async def custom_serve(default_handler):
        async def handler(req):
            if "/media" in req.path:
                media_requests.append(req.path)
            return await default_handler(req)

        return await serve(handler)

    adapter, queue, _, _ = await make_adapter(
        custom_serve, tmp_path, msg, enable_group_summary=True
    )
    try:
        await adapter.process_native_event(native_event(msg))
        assert not adapter._media_tasks
        event = queue.get_nowait()
        assert event.message_str == "总结群聊"
        assert media_requests == []
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "quote_state,embedded,expected_reply",
    [
        ("missing", False, ["引用的图片已失效，请重新发送。"]),
        ("recalled", False, []),
    ],
)
async def test_expired_or_recalled_quoted_image_obeys_quote_protection(
    serve, tmp_path, quote_state, embedded, expected_reply
):
    import asyncio

    msg = message(
        type=49,
        appMessageType=57,
        body="帮我看看这张图",
        quote={
            "serverId": "899",
            "type": 3,
            "state": quote_state,
            "senderId": "author",
            "embeddedImage": embedded,
        },
    )
    adapter, queue, _, sent = await make_adapter(serve, tmp_path, msg)
    try:
        await adapter.process_native_event(native_event(msg))
        await asyncio.gather(*list(adapter._media_tasks))
        assert queue.empty()
        assert [payload["text"] for _, payload in sent] == expected_reply
        assert adapter.state.message_status("m1") == ("replied" if expected_reply else "no_reply")
    finally:
        await adapter.terminate()
