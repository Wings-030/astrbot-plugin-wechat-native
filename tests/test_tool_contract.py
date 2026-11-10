import inspect
import json

import pytest
from conftest import module
from test_adapter import message
from test_history_tools import tool_event

TOOLS = {
    "wechat_search_messages": {"query", "limit", "cursor"},
    "wechat_get_recent_messages": {"limit", "cursor"},
    "wechat_get_group_info": set(),
    "wechat_list_group_members": {"query", "limit", "cursor"},
    "wechat_set_my_group_nickname": {"nickname"},
    "wechat_rename_group": {"name"},
    "wechat_recall_own_message": {"message_id"},
    "wechat_mention_member": {"username", "text"},
}


def test_real_astrbot_registry_exposes_scoped_tools():
    plugin = module("main").WeChatNativePlugin
    from astrbot.core.provider.register import llm_tools

    registered = {t.name: t for t in llm_tools.func_list if t.name.startswith("wechat_")}
    assert set(registered) == set(TOOLS)
    for name, params in TOOLS.items():
        assert set(registered[name].parameters["properties"]) == params
        signature = inspect.signature(getattr(plugin, name))
        assert set(signature.parameters) - {"self", "event"} == params
        if "limit" in params:
            assert signature.parameters["limit"].default == 20
        if "cursor" in params:
            assert signature.parameters["cursor"].default == ""
        assert signature.return_annotation is str
    assert llm_tools.get_func("loop_breaker") is None
    assert llm_tools.get_func("wechat_exit_chat") is None


@pytest.mark.parametrize(
    "name,params",
    [
        ("wechat_get_recent_messages", {}),
        ("wechat_search_messages", {"query": "Docker"}),
        ("wechat_get_group_info", {}),
        ("wechat_list_group_members", {}),
        ("wechat_set_my_group_nickname", {"nickname": "new"}),
        ("wechat_rename_group", {"name": "new"}),
        ("wechat_recall_own_message", {}),
    ],
)
async def test_disabled_and_foreign_tools_make_no_http(serve, tmp_path, name, params):
    adapter, event, _ = await tool_event(serve, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled tool must not access Native")

    adapter.client._request = forbidden
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        for source in (event, object()):
            value = await getattr(plugin, name)(source, **params)
            assert json.loads(value)["status"] == "not_available"
    finally:
        await adapter.terminate()


@pytest.mark.parametrize("unique", [True, False])
async def test_group_subsessions_cannot_expand_history_scope(serve, tmp_path, unique):
    adapter, event, msg = await tool_event(
        serve,
        tmp_path,
        enable_history_tools=True,
        unique_session=unique,
        group_reply_mode="all",
        msg=message(threadId="room@chatroom"),
    )
    calls = []

    async def messages(thread, **kwargs):
        calls.append(thread)
        return {
            "items": [dict(msg, id=str(i), body="中" * 900) for i in range(50)],
            "nextCursor": None,
        }

    adapter.client.messages = messages
    try:
        plugin = object.__new__(module("main").WeChatNativePlugin)
        value = await plugin.wechat_get_recent_messages(event, 50)
        assert len(value.encode()) <= 16384
        data = json.loads(value)
        assert data["truncated"]
        assert data["count"] == len(data["items"]) < 50
        assert calls == ["room@chatroom"]
        event.session.session_id = "a|group|other%40chatroom"
        assert (
            json.loads(await plugin.wechat_get_recent_messages(event))["status"]
            == "permission_denied"
        )
        assert calls == ["room@chatroom"]
    finally:
        await adapter.terminate()


@pytest.mark.parametrize(
    "option,value",
    [
        ("enable_history_tools", 1),
        ("enable_wechat_actions", "yes"),
        ("wechat_action_admins", "all"),
        ("wechat_action_admins", [None]),
        ("admin_token_file", 42),
    ],
)
def test_tool_configuration_rejects_invalid_types(option, value):
    with pytest.raises(ValueError):
        module("config").NativeConfig.from_dict(
            {
                "server_url": "http://localhost:6174",
                "account_id": "a",
                "token": "fake",
                option: value,
            }
        )
