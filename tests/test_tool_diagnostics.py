import json

from aiohttp import web
from astrbot.api import logger  # noqa: F401
from conftest import module
from test_history_tools import tool_event


async def test_disabled_history_logs_runtime_reason_without_request(serve, tmp_path, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["status"] == "not_available"
        assert "reason=history_tools_disabled" in caplog.text
    finally:
        await adapter.terminate()


async def test_native_failure_logs_status_and_safe_code_without_body(serve, tmp_path, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)

    async def handler(req):
        return web.json_response(
            {"error": {"code": "not_found", "message": "synthetic-secret-and-chat-body"}},
            status=404,
        )

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_search_messages(event, "synthetic-query"))
        assert result["status"] == "not_available"
        assert "http=404 code=not_found" in caplog.text
        assert "synthetic-secret-and-chat-body" not in caplog.text
        assert "http" not in result and "code" not in result
    finally:
        await adapter.terminate()


async def test_foreign_event_logs_event_mismatch(caplog):
    plugin = object.__new__(module("main").WeChatNativePlugin)
    result = json.loads(await plugin.wechat_get_recent_messages(object()))
    assert result["status"] == "not_available"
    assert "reason=event_type_mismatch" in caplog.text


async def test_reloaded_tool_class_accepts_adapter_owned_event(serve, tmp_path, monkeypatch):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    support = module("tool_support")
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(support, "WeChatNativeEvent", reloaded_type)
    assert not isinstance(event, reloaded_type)

    async def messages(*args, **kwargs):
        return {"items": [msg], "nextCursor": None}

    adapter.client.messages = messages
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["ok"] is True
        assert result["count"] == 1
    finally:
        await adapter.terminate()


async def test_reload_fallback_rejects_unowned_event(serve, tmp_path, monkeypatch, caplog):
    adapter, event, _ = await tool_event(serve, tmp_path, enable_history_tools=True)
    support = module("tool_support")
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(support, "WeChatNativeEvent", reloaded_type)

    # Same class and message metadata, but not retained by the Native adapter.
    impersonator = original_type(event.message_obj, adapter)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(impersonator))
        assert result["status"] == "not_available"
        assert "reason=event_type_mismatch" in caplog.text
    finally:
        await adapter.terminate()


async def test_reinjected_event_keeps_adapter_ownership(serve, tmp_path, monkeypatch):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    original_type = type(event)
    reloaded_type = type(
        "WeChatNativeEvent",
        (original_type,),
        {"__module__": original_type.__module__},
    )
    monkeypatch.setattr(module("tool_support"), "WeChatNativeEvent", reloaded_type)

    async def messages(*args, **kwargs):
        return {"items": [msg], "nextCursor": None}

    adapter.client.messages = messages
    try:
        reinjected = adapter.create_event(event.message_obj)
        assert adapter._events[msg["id"]] is reinjected
        plugin = object.__new__(module("main").WeChatNativePlugin)
        accepted = json.loads(await plugin.wechat_get_recent_messages(reinjected))
        assert accepted["ok"] is True
        rejected = json.loads(await plugin.wechat_get_recent_messages(event))
        assert rejected["status"] == "not_available"
    finally:
        await adapter.terminate()
