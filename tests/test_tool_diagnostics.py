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
