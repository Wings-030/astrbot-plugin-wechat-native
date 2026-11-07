import json

import pytest
from aiohttp import web
from conftest import module
from test_adapter import make_adapter, native_event


async def tool_event(serve, tmp_path, **options):
    adapter, queue, msg, _ = await make_adapter(serve, tmp_path, **options)
    await adapter.process_native_event(native_event(msg))
    return adapter, queue.get_nowait(), msg


async def test_recent_history_is_scoped_and_disabled_by_default(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["status"] == "not_available"
        assert adapter.state.cursor == "1"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("unique", [True, False])
async def test_recent_page_hides_recall_and_binds_cursor(serve, tmp_path, unique):
    adapter, event, msg = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        unique_session=unique,
    )
    calls = []

    async def page(thread, *, limit, cursor="", query=""):
        calls.append((thread, limit, cursor, query))
        return {
            "items": [dict(msg, body="secret", recall={}), dict(msg, id="m2", body="a" * 900)],
            "nextCursor": "native-next",
        }

    adapter.client.messages = page
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        result = json.loads(await plugin.wechat_get_recent_messages(event))
        assert result["ok"]
        assert result["items"][0]["text"] == "[已撤回]"
        assert len(result["items"][1]["text"]) == 800
        assert "secret" not in json.dumps(result)
        await plugin.wechat_get_recent_messages(event, cursor=result["nextCursor"])
        assert calls[-1] == ("peer", 20, "native-next", "")
        bad = json.loads(await plugin.wechat_get_recent_messages(event, 10, result["nextCursor"]))
        assert bad["status"] == "invalid_cursor"
    finally:
        await adapter.terminate()


async def test_recent_uses_native_http_and_preserves_checkpoint(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    requests = []

    async def handler(req):
        requests.append((req.method, req.path, dict(req.query)))
        return web.json_response({"items": [msg], "nextCursor": None})

    adapter.client.api_url = await serve(handler)
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        data = json.loads(await plugin.wechat_get_recent_messages(event))
        assert data["items"][0]["text"] == "你好"
        assert requests == [("GET", "/accounts/a/threads/peer/messages", {"limit": "20"})]
        assert adapter.state.cursor == "1"
        for invalid in (True, 1.5, 0, 51, "20"):
            data = json.loads(await plugin.wechat_get_recent_messages(event, invalid))
            assert data["status"] == "invalid_request"
        assert len(requests) == 1
    finally:
        await adapter.terminate()


async def test_foreign_and_tampered_events_cannot_read(serve, tmp_path):
    adapter, event, msg = await tool_event(serve, tmp_path, enable_history_tools=True)
    calls = []

    async def page(*args, **kwargs):
        calls.append(True)
        return {"items": [dict(msg, threadId="other")], "nextCursor": None}

    adapter.client.messages = page
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        assert (
            json.loads(await plugin.wechat_get_recent_messages(object()))["status"]
            == "not_available"
        )
        assert (
            json.loads(await plugin.wechat_get_recent_messages(event))["status"] == "not_available"
        )
        assert len(calls) == 1
        event.message_obj.raw_message["threadId"] = "other"
        assert (
            json.loads(await plugin.wechat_get_recent_messages(event))["status"]
            == "permission_denied"
        )
        assert len(calls) == 1
    finally:
        await adapter.terminate()
