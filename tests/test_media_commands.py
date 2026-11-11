import asyncio
from types import SimpleNamespace

import pytest
from conftest import module
from test_adapter import native_event, no_telemetry  # noqa: F401
from test_recent_images import make_recent_adapter


async def test_media_commands_appear_in_real_management_list(monkeypatch):
    from astrbot.core.star import command_management

    module("main")

    async def no_overrides():
        return []

    monkeypatch.setattr(command_management.db_helper, "get_command_configs", no_overrides)
    commands = await command_management.list_commands()
    listed = {
        item["effective_command"]: item
        for item in commands
        if item["module_path"] == "wechat_plugin.main"
    }
    for name in ("读取附件", "读取图片", "读取语音", "读取文档"):
        assert name in listed
        assert listed[name]["type"] == "command"
        assert listed[name]["description"]
    assert "读取PDF" in listed["读取文档"]["aliases"]


@pytest.mark.parametrize("setting", ["disabled", "renamed"])
async def test_management_changes_control_attachment_lookup(serve, tmp_path, monkeypatch, setting):
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    module("main")
    handler = star_handlers_registry.get_handler_by_full_name("wechat_plugin.main_read_image")
    assert handler is not None
    if setting == "disabled":
        monkeypatch.setattr(handler, "enabled", False)
    else:
        command = next(f for f in handler.event_filters if isinstance(f, CommandFilter))
        monkeypatch.setattr(command, "command_name", "分析图像")
        monkeypatch.setattr(command, "_cmpl_cmd_names", None)
    adapter, queue, image, request, sent, paths, _ = await make_recent_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(image, "1"))
        await adapter.process_native_event(native_event(request, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        event = queue.get_nowait()
        assert event.get_extra("wechat_native_media_kind") is None
        assert not any(path.endswith("/media") for path in paths)
        assert sent == []
        if setting == "renamed":
            request.update(id="renamed-request", body="@机器人\u2005分析图像，描述内容")
            await adapter.process_native_event(native_event(request, "3"))
            await asyncio.gather(*list(adapter._media_tasks))
            assert queue.get_nowait().get_extra("wechat_native_media_kind") == "image"
    finally:
        await adapter.terminate()


async def test_registered_media_handler_preserves_default_llm_delivery(
    serve, tmp_path, monkeypatch
):
    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.star.star_handler import star_handlers_registry

    plugin = object.__new__(module("main").WeChatNativePlugin)
    handler = star_handlers_registry.get_handler_by_full_name("wechat_plugin.main_read_image")
    assert handler is not None
    monkeypatch.setattr(handler, "handler", plugin.read_image)
    adapter, queue, image, request, sent, paths, _ = await make_recent_adapter(serve, tmp_path)
    received = []

    class Agent:
        async def process(self, event):
            received.append(event)
            yield

    try:
        await adapter.process_native_event(native_event(image, "1"))
        await adapter.process_native_event(native_event(request, "2"))
        await asyncio.gather(*list(adapter._media_tasks))
        event = queue.get_nowait()
        event.is_at_or_wake_command = True
        event.set_extra("activated_handlers", [handler])
        event.set_extra("handlers_parsed_params", {})
        stage = ProcessStage()
        stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
        stage.star_request_sub_stage = StarRequestSubStage()
        stage.agent_sub_stage = Agent()
        async for _ in stage.process(event):
            pass
        assert received == [event]
        assert event.get_extra("wechat_native_media_kind") == "image"
        assert not event.is_stopped()
        assert len([path for path in paths if path.endswith("/media")]) == 1
        assert sent == []
    finally:
        await adapter.terminate()
