import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import module
from test_adapter import (
    make_adapter,
    message,
    native_event,
    no_telemetry,  # noqa: F401
)


def group_message(**updates):
    return message(threadId="room@chatroom", **updates)


def test_summary_is_opt_in_and_limits_are_configurable():
    config = module("config")
    base = {"server_url": "http://localhost:6174", "account_id": "a"}
    assert config.DEFAULT_CONFIG.get("enable_group_summary") is False
    cfg = config.NativeConfig.from_dict(
        {
            **base,
            "enable_group_summary": True,
            "summary_max_messages": 3,
            "summary_window_minutes": 5,
        }
    )
    assert cfg.enable_group_summary
    cache = module("summary").GroupSummaryCache(
        cfg.summary_max_messages, cfg.summary_window_minutes
    )
    for i in range(5):
        cache.add(group_message(id=f"m{i}", body=f"讨论{i}"))
    assert [record.text for record in cache.snapshot("room@chatroom")] == [
        "讨论2",
        "讨论3",
        "讨论4",
    ]


@pytest.mark.parametrize(
    "option,value",
    [
        ("enable_group_summary", "true"),
        ("summary_max_messages", 0),
        ("summary_max_messages", 501),
        ("summary_max_messages", True),
        ("summary_window_minutes", 0),
        ("summary_window_minutes", 1441),
        ("summary_window_minutes", 1.5),
    ],
)
def test_invalid_summary_config_is_rejected(option, value):
    with pytest.raises(ValueError):
        module("config").NativeConfig.from_dict(
            {
                "server_url": "http://localhost:6174",
                "account_id": "a",
                option: value,
            }
        )


def test_cache_expires_records_and_bounds_all_groups_and_prompt(monkeypatch):
    summary = module("summary")
    now = time.time()
    monkeypatch.setattr(summary.time, "time", lambda: now)
    cache = summary.GroupSummaryCache(500, 60)
    for i in range(2100):
        cache.add(message(id=f"m{i}", threadId=f"room{i % 5}@chatroom", body="长消息" * 1000))
    records = tuple(record for i in range(5) for record in cache.snapshot(f"room{i}@chatroom"))
    assert len(records) == 2000
    assert all(len(record.text) <= 1000 for record in records)
    prompt = summary.summary_prompt(cache.snapshot("room0@chatroom"))
    assert len(prompt) <= 24000
    assert "截断" in prompt
    assert "m2095" in prompt  # Newest available record remains in the model input.
    now += 3601
    assert cache.snapshot("room0@chatroom") == ()


async def test_passive_collection_does_not_dispatch_and_excludes_other_groups(serve, tmp_path):
    adapter, queue, msg, _ = await make_adapter(
        serve,
        tmp_path,
        group_message(body="周五发布"),
        enable_group_summary=True,
        allowed_groups=["room@chatroom"],
    )
    try:
        await adapter.process_native_event(native_event(msg))
        assert queue.empty()
        msg.update(id="m2", serverId="901", senderId="other", body="我来准备文档")
        await adapter.process_native_event(native_event(msg, "2"))
        msg.update(id="m3", threadId="other@chatroom", body="别的群秘密")
        await adapter.process_native_event(native_event(msg, "3"))
        records = adapter.summary_cache.snapshot("room@chatroom")
        assert [r.text for r in records] == ["周五发布", "我来准备文档"]
        assert adapter.summary_cache.snapshot("other@chatroom") == ()
        assert queue.empty()
        assert (
            "周五发布"
            not in adapter.state.db.execute("SELECT * FROM messages").fetchall().__str__()
        )
    finally:
        await adapter.terminate()
    assert adapter.summary_cache.snapshot("room@chatroom") == ()


@pytest.mark.parametrize(
    "updates,options,origin",
    [
        ({}, {}, "history"),
        ({}, {}, "unknown"),
        ({"type": 3}, {}, "live"),
        ({"senderId": "bot"}, {}, "live"),
        ({"direction": "outgoing"}, {}, "live"),
        ({"recall": {"observedAt": "now"}}, {}, "live"),
        ({"createdAt": (datetime.now(UTC) - timedelta(minutes=6)).isoformat()}, {}, "live"),
        ({}, {"enable_group_summary": False}, "live"),
        ({}, {"group_reply_mode": "disabled"}, "live"),
        ({"threadId": "peer"}, {}, "live"),
    ],
)
async def test_ineligible_messages_are_not_cached(serve, tmp_path, updates, options, origin):
    msg = group_message()
    msg.update(updates)
    adapter, _, _, _ = await make_adapter(
        serve,
        tmp_path,
        msg,
        **{"enable_group_summary": True, **options},
    )
    try:
        await adapter.process_native_event(native_event(msg, origin=origin))
        assert adapter.summary_cache.snapshot("room@chatroom") == ()
        assert adapter.summary_cache.snapshot("peer") == ()
    finally:
        await adapter.terminate()


async def test_duplicates_and_recalls_are_removed_from_summary(serve, tmp_path):
    adapter, _, msg, _ = await make_adapter(
        serve,
        tmp_path,
        group_message(),
        enable_group_summary=True,
    )
    try:
        await adapter.process_native_event(native_event(msg))
        await adapter.process_native_event(native_event(msg, "2"))
        assert len(adapter.summary_cache.snapshot("room@chatroom")) == 1
        await adapter.process_native_event(
            {
                "cursor": "3",
                "kind": "message.recalled",
                "data": {"threadId": "room@chatroom", "serverId": "900", "messageId": "m1"},
            }
        )
        assert adapter.summary_cache.snapshot("room@chatroom") == ()
    finally:
        await adapter.terminate()


class SummaryContext:
    def __init__(self, response="讨论要点：周五发布；待办：准备文档。", error=None):
        self.response = response
        self.error = error
        self.requests = []
        self.umos = []

    def register_web_api(self, *args):
        pass

    async def get_current_chat_provider_id(self, umo):
        self.umos.append(umo)
        return "selected-model"

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text=self.response)


async def summary_command(adapter, queue, msg, cursor="2", **updates):
    msg.update(
        id=f"cmd{cursor}",
        serverId=f"cmd-server{cursor}",
        body="@机器人\u2005总结群聊",
        mentions={"state": "known", "self": True},
        **updates,
    )
    await adapter.process_native_event(native_event(msg, cursor))
    return queue.get_nowait()


async def deliver_summary(plugin, event):
    results = []
    async for result in plugin.summarize_group(event):
        results.append(result)
        await event.send(result)
    return results


async def test_summary_command_uses_current_provider_and_sends_once(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(body="周五发布"),
        enable_group_summary=True,
    )
    context = SummaryContext()
    plugin = module("main").WeChatNativePlugin(context)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await summary_command(adapter, queue, msg)
        assert len(adapter.summary_cache.snapshot("room@chatroom")) == 1
        await deliver_summary(plugin, event)
        assert context.umos == [event.unified_msg_origin]
        request = context.requests[0]
        assert request["chat_provider_id"] == "selected-model"
        assert "周五发布" in request["prompt"]
        assert "总结群聊" not in request["prompt"]
        assert "待办" in request["system_prompt"]
        assert not request.get("contexts") and not request.get("tools")
        assert len(sent) == 1
        assert context.response in sent[0][1]["text"]
        assert sent[0][1]["threadId"] == "room@chatroom"
        assert event.is_stopped()
        assert adapter.state.message_status(event.message_obj.message_id) == "replied"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "options,response,error,expected",
    [
        ({}, "回答", None, "开启"),
        ({"enable_group_summary": True}, "", None, "空"),
        ({"enable_group_summary": True}, "回答", RuntimeError("private-token"), "失败"),
        ({"enable_group_summary": True}, "回答", TimeoutError("private-token"), "超时"),
    ],
)
async def test_summary_errors_have_clear_replies_without_details(
    serve,
    tmp_path,
    options,
    response,
    error,
    expected,
):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path, group_message(), **options)
    context = SummaryContext(response, error)
    plugin = module("main").WeChatNativePlugin(context)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await summary_command(adapter, queue, msg)
        await deliver_summary(plugin, event)
        assert expected in sent[0][1]["text"]
        assert "private-token" not in json.dumps(sent)
        assert event.is_stopped()
    finally:
        await adapter.terminate()


async def test_empty_cache_does_not_call_model(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(),
        enable_group_summary=True,
    )
    context = SummaryContext()
    plugin = module("main").WeChatNativePlugin(context)
    try:
        event = await summary_command(adapter, queue, msg, "1")
        await deliver_summary(plugin, event)
        assert "暂无" in sent[0][1]["text"]
        assert context.requests == []
    finally:
        await adapter.terminate()


async def test_command_requires_real_group_mention_even_in_all_mode(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(body="总结群聊"),
        enable_group_summary=True,
        group_reply_mode="all",
    )
    context = SummaryContext()
    plugin = module("main").WeChatNativePlugin(context)
    try:
        await adapter.process_native_event(native_event(msg))
        await deliver_summary(plugin, queue.get_nowait())
        msg.update(id="private-command", threadId="peer")
        await adapter.process_native_event(native_event(msg, "2"))
        await deliver_summary(plugin, queue.get_nowait())
        assert not context.requests and not sent
    finally:
        await adapter.terminate()


async def test_recall_during_generation_prevents_stale_summary(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(body="已撤回内容"),
        enable_group_summary=True,
    )
    context = SummaryContext(response="不能发出的旧摘要")
    plugin = module("main").WeChatNativePlugin(context)
    original_generate = context.llm_generate

    async def generate(**kwargs):
        await adapter.process_native_event(
            {
                "cursor": "3",
                "kind": "message.recalled",
                "data": {"threadId": "room@chatroom", "serverId": "900", "messageId": "m1"},
            }
        )
        return await original_generate(**kwargs)

    context.llm_generate = generate
    try:
        await adapter.process_native_event(native_event(msg))
        event = await summary_command(adapter, queue, msg)
        await deliver_summary(plugin, event)
        assert "不能发出的旧摘要" not in sent[0][1]["text"]
        assert "重试" in sent[0][1]["text"]
    finally:
        await adapter.terminate()


async def test_summary_command_registered_with_astrbot_alias():
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    module("main")
    handlers = [h for h in star_handlers_registry if h.handler_name == "summarize_group"]
    assert handlers
    commands = [f for f in handlers[-1].event_filters if isinstance(f, CommandFilter)]
    assert commands[0].equals("总结群聊")
    assert commands[0].equals("群聊摘要")


async def test_same_group_summary_requests_are_serialized(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(body="周五发布"),
        enable_group_summary=True,
    )
    started, release = asyncio.Event(), asyncio.Event()
    context = SummaryContext()
    plugin = module("main").WeChatNativePlugin(context)
    original_generate = context.llm_generate

    async def generate(**kwargs):
        started.set()
        await release.wait()
        return await original_generate(**kwargs)

    context.llm_generate = generate
    task = None
    try:
        await adapter.process_native_event(native_event(msg))
        event = await summary_command(adapter, queue, msg)
        task = asyncio.create_task(plugin._generate_group_summary(event))
        await asyncio.wait_for(started.wait(), 1)
        second = await summary_command(adapter, queue, msg, "3")
        await deliver_summary(plugin, second)
        assert "正在生成" in sent[0][1]["text"]
        release.set()
        assert context.response in await task
        assert context.response in await plugin._generate_group_summary(second)
        assert len(context.requests) == 2
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await adapter.terminate()


async def test_summary_sources_rechecked_at_send_boundary(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(
        serve,
        tmp_path,
        group_message(body="需要撤回的内容"),
        enable_group_summary=True,
    )
    context = SummaryContext(response="旧摘要")
    plugin = module("main").WeChatNativePlugin(context)
    try:
        await adapter.process_native_event(native_event(msg))
        event = await summary_command(adapter, queue, msg)
        handler = plugin.summarize_group(event)
        result = await anext(handler)
        original_message = adapter.client.message

        async def read_source(message_id):
            await adapter.process_native_event(
                {
                    "cursor": "3",
                    "kind": "message.recalled",
                    "data": {"threadId": "room@chatroom", "serverId": "900", "messageId": "m1"},
                }
            )
            return await original_message(message_id)

        adapter.client.message = read_source
        await event.send(result)
        await handler.aclose()
        assert sent == []
        assert adapter.state.message_status(event.message_obj.message_id) == "no_reply"
    finally:
        await adapter.terminate()
