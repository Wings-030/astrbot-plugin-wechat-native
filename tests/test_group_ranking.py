import asyncio
from datetime import datetime, timedelta

from aiohttp import web
from conftest import module
from test_adapter import make_adapter, message, native_event, no_telemetry  # noqa: F401
from test_native_client import member_lookup
from test_summary import SummaryContext


def text_message(message_id, sender="alpha", **updates):
    return message(id=message_id, threadId="room@chatroom", senderId=sender, **updates)


async def ranking_session(serve, tmp_path, pages, **options):
    reads = []
    names = {"alpha": "潜水冠军", "bravo": "潜水冠军", "bot": "今日值班生"}

    async def native_server(handler):
        async def native(req):
            if "/threads/" in req.path and req.path.endswith("/messages"):
                reads.append((req.path, dict(req.query)))
                page = pages[int(req.query.get("cursor", "0"))]
                return page if isinstance(page, web.Response) else web.json_response(page)
            if "/members/" in req.path:
                username = req.path.rsplit("/", 1)[-1]
                return web.json_response(
                    member_lookup(
                        username,
                        member={"username": username, "roomDisplayName": names.get(username)},
                    )
                )
            return await handler(req)

        return await serve(native)

    command = text_message(
        "request", body="@任何机器人\u2005 谁最能聊 ", mentions={"state": "known", "self": True}
    )
    adapter, queue, msg, sent = await make_adapter(
        native_server, tmp_path, command, group_reply_mode="all", **options
    )
    context = SummaryContext()
    plugin = module("main").WeChatNativePlugin(context)
    await adapter.process_native_event(native_event(msg))
    return adapter, queue, msg, sent, reads, plugin, context


async def test_ranking_only_handles_exact_genuine_group_mentions(serve, tmp_path):
    adapter, queue, msg, sent, reads, plugin, context = await ranking_session(
        serve, tmp_path, [{"items": [], "nextCursor": None}]
    )
    try:
        event = queue.get_nowait()
        from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter
        from astrbot.core.star.star_handler import star_handlers_registry

        registered = [h for h in star_handlers_registry if h.handler_name == "rank_group"][-1]
        assert any(isinstance(f, EventMessageTypeFilter) for f in registered.event_filters)
        for index, (body, thread, mentioned) in enumerate(
            [
                ("谁最能聊", "peer", True),
                ("@机器人 谁最能聊", "room@chatroom", False),
                ("谁最能聊", "room@chatroom", False),
                ("/谁最能聊", "room@chatroom", True),
                ("谁最能聊 昨天", "room@chatroom", True),
            ],
            2,
        ):
            msg.update(
                id=f"wrong{index}",
                threadId=thread,
                body=body,
                mentions={"state": "known", "self": mentioned},
            )
            await adapter.process_native_event(native_event(msg, str(index)))
            ignored = queue.get_nowait()
            await plugin.rank_group(ignored)
            assert not ignored.is_stopped()
        assert not sent and not reads and not context.requests
        msg.update(event.message_obj.raw_message)
        await plugin.rank_group(event)
        assert len(sent) == 1
        assert set(sent[0][1]) == {"threadId", "text"}
        assert sent[0][1]["text"] == "今天暂无可统计的聊天消息。"
        assert event.is_stopped()
    finally:
        await adapter.terminate()


async def test_ranking_counts_today_texts_and_renders_top_five_across_pages(serve, tmp_path):
    yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    rows = [
        text_message(f"{sender}{index}", sender)
        for sender, count in [
            ("alpha", 3),
            ("bravo", 3),
            ("charlie", 2),
            ("delta", 1),
            ("echo", 1),
            ("foxtrot", 1),
        ]
        for index in range(count)
    ]
    excluded = [
        text_message("old", createdAt=yesterday),
        text_message("self", "bot"),
        text_message("out", direction="outgoing"),
        text_message("image", type=3),
        text_message("recall", recall={"observedAt": "now"}),
        text_message("deleted", deleted=True),
        text_message("empty", body=" \n"),
        text_message("missing-time", createdAt=None),
        text_message("missing-sender", None),
        text_message(
            "command", body="@昵称\u2005谁最能聊", mentions={"state": "known", "self": True}
        ),
    ]
    adapter, queue, _, sent, reads, plugin, _ = await ranking_session(
        serve,
        tmp_path,
        [
            {"items": rows[:5], "nextCursor": "1"},
            {"items": [rows[0], *rows[5:], *excluded], "nextCursor": None},
        ],
    )
    try:
        await plugin.rank_group(queue.get_nowait())
        text = sent[0][1]["text"]
        assert text.splitlines()[:7] == [
            "谁最能聊",
            "",
            "1. 潜水冠军（alpha）：3 条",
            "2. 潜水冠军（bravo）：3 条",
            "3. charlie：2 条",
            "4. delta：1 条",
            "5. echo：1 条",
        ]
        assert "foxtrot" not in text
        assert "仅供娱乐" not in text and "已保存" not in text
        assert reads == [
            ("/v1/accounts/a/threads/room@chatroom/messages", {"limit": "50"}),
            ("/v1/accounts/a/threads/room@chatroom/messages", {"limit": "50", "cursor": "1"}),
        ]
    finally:
        await adapter.terminate()


async def test_ranking_empty_and_failed_history_have_friendly_single_replies(serve, tmp_path):
    pages = [{"items": [], "nextCursor": None}]
    adapter, queue, msg, sent, reads, plugin, context = await ranking_session(
        serve, tmp_path, pages
    )
    try:
        await plugin.rank_group(queue.get_nowait())
        assert sent[0][1]["text"] == "今天暂无可统计的聊天消息。"
        pages[0] = web.json_response({"message": "private-token"}, status=403)
        msg.update(id="failed-query", serverId="901")
        await adapter.process_native_event(native_event(msg, "2"))
        failed = queue.get_nowait()
        await plugin.rank_group(failed)
        assert sent[1][1]["text"] == "暂时查不到群聊记录，请稍后再试。"
        assert len(sent) == len(reads) == 2
        assert not context.requests
        assert failed.is_stopped()
    finally:
        await adapter.terminate()


async def test_ranking_duplicate_event_only_queries_and_sends_once(serve, tmp_path):
    adapter, queue, msg, sent, reads, plugin, context = await ranking_session(
        serve, tmp_path, [{"items": [text_message("chat")], "nextCursor": None}]
    )
    try:
        event = queue.get_nowait()
        duplicate = adapter.create_event(event.message_obj)
        await asyncio.gather(
            plugin.rank_group(event), plugin.rank_group(event), plugin.rank_group(duplicate)
        )
        await plugin.rank_group(event)
        reinjected = adapter.create_event(event.message_obj)
        await plugin.rank_group(reinjected)
        await adapter.process_native_event(native_event(msg, "2"))
        assert queue.empty()
        assert len(reads) == len(sent) == 1
        assert event.is_stopped() and reinjected.is_stopped()
        assert event._has_send_oper and not event.get_result().chain
        assert not context.requests
        assert "1. 潜水冠军：1 条" in sent[0][1]["text"]
    finally:
        await adapter.terminate()
