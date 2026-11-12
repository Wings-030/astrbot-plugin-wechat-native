"""Cross-platform commands and durable, at-most-once incident notifications."""

import asyncio
from types import SimpleNamespace

import pytest
from conftest import module
from test_connection_recovery import MemoryKV, robot


def chat_event(platform_type="telegram", platform_id="tg-main", *, admin=True, private=True):
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    event = AstrMessageEvent(
        "/bind_alert",
        message,
        PlatformMetadata(platform_type, "test", id=platform_id),
        "private-sensitive-user",
    )
    event.role = "admin" if admin else "member"
    return event


class AlertContext:
    def __init__(self, platform_type="telegram", platform_id="tg-main"):
        self.robots = [robot(), {"id": platform_id, "type": platform_type, "enable": True}]
        self.platform = SimpleNamespace(
            config=self.robots[1],
            meta=lambda: SimpleNamespace(id=platform_id, name=platform_type),
        )
        self.sent = []
        self.send_result = True
        self.before_send = None

    def get_config(self):
        return {"platform": self.robots}

    def get_platform_inst(self, platform_id):
        if self.platform and self.platform.meta().id == platform_id:
            return self.platform
        return None

    async def send_message(self, umo, chain):
        if self.before_send:
            self.before_send()
        self.sent.append((umo, chain.get_plain_text()))
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return self.send_result


def services(context=None, kv=None, probe=None):
    context, kv = context or AlertContext(), kv or MemoryKV()
    bindings = module("alert_binding").AlertBindingService(context, kv)
    sender = module("alert_sender").AlertSender(context)
    monitor = module("connection_monitor").ConnectionMonitor(
        context, kv, bindings, sender, probe=probe
    )
    return context, kv, bindings, sender, monitor


@pytest.mark.parametrize("platform", ["telegram", "weixin_oc", "line", "aiocqhttp"])
async def test_binding_persists_exact_trusted_private_origin_and_is_idempotent(platform):
    context = AlertContext(platform, "receiver-main")
    _, kv, bindings, _, _ = services(context)
    event = chat_event(platform, "receiver-main")
    assert "已绑定" in await bindings.execute("bind", event)
    value = await bindings.get("native-1")
    assert value["platform_type"] == platform
    assert value["platform_id"] == "receiver-main"
    assert value["umo"] == event.unified_msg_origin
    await bindings.execute("bind", event)
    assert await bindings.get("native-1") == value
    restored = module("alert_binding").AlertBindingService(context, kv)
    assert await restored.get("native-1") == value
    status = await bindings.execute("status", event)
    assert "private-sensitive-user" not in status


@pytest.mark.parametrize("action", ["bind", "unbind", "status", "test"])
@pytest.mark.parametrize("admin,private", [(False, True), (True, False)])
async def test_all_commands_reject_untrusted_or_group_permission(action, admin, private):
    context, kv, bindings, _, _ = services()
    result = await bindings.execute(action, chat_event(admin=admin, private=private))
    assert "管理员私聊" in result
    assert kv.data == {} and context.sent == []


async def test_command_target_selection_overwrite_unbind_and_failed_storage():
    context, kv, bindings, _, _ = services()
    event = chat_event()
    context.robots.append(robot(id="native-2"))
    result = await bindings.execute("bind", event)
    assert "native-1" in result and "native-2" in result
    assert not kv.data
    assert "已绑定" in await bindings.execute("bind", event, "native-2")
    assert "已绑定" in await bindings.execute("bind", event, "native-1")
    assert "已解绑" in await bindings.execute("unbind", event, "native-1")
    assert await bindings.get("native-1") is None
    assert await bindings.get("native-2") is not None
    context.robots[0]["enable"] = False
    assert "已启用" in await bindings.execute("bind", event, "native-1")
    assert "已启用" in await bindings.execute("bind", event, "unknown")
    kv.fail = True
    assert "已绑定" not in await bindings.execute("bind", event, "native-2")


@pytest.mark.parametrize("failure", [False, RuntimeError("SENSITIVE UMO AND TOKEN")])
async def test_manual_notification_reports_failure_without_incident_changes(failure):
    context, kv, bindings, _, _ = services()
    event = chat_event()
    await bindings.execute("bind", event)
    before = dict(kv.data)
    context.send_result = failure
    text = await bindings.execute("test", event)
    assert "失败" in text and "SENSITIVE" not in text
    assert len(context.sent) == 1
    assert kv.data == before


async def test_sender_refuses_missing_disabled_or_changed_platform_without_fallback():
    context, _, bindings, sender, _ = services()
    await bindings.execute("bind", chat_event())
    binding = await bindings.get("native-1")
    for platform in (
        None,
        SimpleNamespace(
            config={"enable": False}, meta=lambda: SimpleNamespace(id="tg-main", name="telegram")
        ),
        SimpleNamespace(
            config={"enable": True}, meta=lambda: SimpleNamespace(id="tg-main", name="line")
        ),
    ):
        context.platform = platform
        assert await sender.send(binding, "native-1", "test") != "sent"
    assert context.sent == []


class Observations:
    def __init__(self):
        self.health = "offline"
        self.calls = 0

    async def __call__(self, bot):
        self.calls += 1
        return module("connection_probe").ProbeResult(self.health, failure_kind="unauthenticated")


async def ticks(monitor, count):
    for _ in range(count):
        await monitor.check_all()


async def test_monitor_thresholds_attempt_before_send_restart_and_new_incident():
    probe = Observations()
    context, kv, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())

    def attempted_first():
        state = kv.data["connection_incident:native-1"]
        kind = "outage" if state["phase"] == "INCIDENT_OPEN" else "recovery"
        assert state[kind + "_attempted"] is True

    context.before_send = attempted_first
    await ticks(monitor, 2)
    assert context.sent == []
    await ticks(monitor, 5)
    assert len(context.sent) == 1 and "已断线" in context.sent[0][1]
    first_id = (await monitor.get_state(context.robots[0]))["incident_id"]
    monitor = services(context, kv, probe)[-1]
    await ticks(monitor, 3)
    assert len(context.sent) == 1
    probe.health = "online"
    await ticks(monitor, 1)
    assert len(context.sent) == 1
    await ticks(monitor, 3)
    assert len(context.sent) == 2 and "已恢复" in context.sent[-1][1]
    probe.health = "offline"
    await ticks(monitor, 3)
    assert len(context.sent) == 3
    assert (await monitor.get_state(context.robots[0]))["incident_id"] != first_id


async def test_monitor_consecutive_counts_reset_and_first_online_is_only_baseline():
    probe = Observations()
    context, _, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())
    probe.health = "online"
    await ticks(monitor, 2)
    assert context.sent == []
    probe.health = "offline"
    await ticks(monitor, 2)
    probe.health = "online"
    await ticks(monitor, 1)
    probe.health = "offline"
    await ticks(monitor, 2)
    assert context.sent == []
    await ticks(monitor, 1)
    probe.health = "online"
    await ticks(monitor, 1)
    probe.health = "unavailable"
    await ticks(monitor, 1)
    probe.health = "online"
    await ticks(monitor, 1)
    assert len(context.sent) == 1
    await ticks(monitor, 1)
    assert len(context.sent) == 2


async def test_no_binding_then_rebind_does_not_backfill_but_recovery_uses_current_binding():
    probe = Observations()
    context, kv, bindings, _, monitor = services(probe=probe)
    await ticks(monitor, 3)
    assert probe.calls == 3 and context.sent == []
    assert kv.data["connection_incident:native-1"]["last_outage_result"] == "skipped_no_binding"
    await bindings.execute("bind", chat_event())
    await ticks(monitor, 4)
    assert context.sent == []
    probe.health = "online"
    await ticks(monitor, 2)
    assert len(context.sent) == 1 and "已恢复" in context.sent[0][1]


async def test_failed_send_and_kv_write_do_not_retry_event():
    probe = Observations()
    context, kv, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())
    context.send_result = False
    await ticks(monitor, 5)
    assert len(context.sent) == 1
    assert kv.data["connection_incident:native-1"]["last_outage_result"] == "failed"
    probe.health = "online"
    await ticks(monitor, 1)
    kv.fail = True
    await ticks(monitor, 1)
    assert len(context.sent) == 1
    kv.fail = False
    await ticks(monitor, 3)
    assert len(context.sent) == 1


async def test_disabled_removed_and_reconfigured_bots_cannot_send_false_recovery():
    probe = Observations()
    context, _, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())
    await ticks(monitor, 3)
    assert len(context.sent) == 1
    context.robots[0]["enable"] = False
    await ticks(monitor, 3)
    assert probe.calls == 3
    context.robots[0].update(enable=True, account_id="new-account")
    probe.health = "online"
    await ticks(monitor, 3)
    assert len(context.sent) == 1
    context.robots.pop(0)
    await ticks(monitor, 1)
    assert probe.calls == 6


async def test_parallel_monitor_checks_serialize_and_stop_cancels_all_tasks():
    probe = Observations()
    context, _, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())
    await asyncio.gather(*(monitor.check_all() for _ in range(6)))
    assert len(context.sent) == 1
    monitor.start()
    task = monitor.task
    await asyncio.sleep(0)
    await monitor.stop()
    assert task.done()
    assert monitor.task is None


async def test_aliases_share_handlers_and_ascii_primary_commands(monkeypatch):
    from astrbot.core.star import command_management

    module("main")

    async def no_overrides():
        return []

    monkeypatch.setattr(command_management.db_helper, "get_command_configs", no_overrides)
    commands = {
        c["effective_command"]: c
        for c in await command_management.list_commands()
        if c["module_path"] == "wechat_plugin.main"
    }
    for command, alias in (
        ("bind_alert", "绑定通知"),
        ("unbind_alert", "解绑通知"),
        ("alert_status", "通知状态"),
        ("test_alert", "测试通知"),
    ):
        assert command in commands
        assert alias in commands[command]["aliases"]


async def test_binding_storage_value_error_is_never_returned_to_chat():
    context, kv, bindings, _, _ = services()

    async def broken_write(*_):
        raise ValueError("SENSITIVE TOKEN AND UMO")

    kv.put_kv_data = broken_write
    result = await bindings.execute("bind", chat_event())
    assert "SENSITIVE" not in result
    assert "失败" in result and context.sent == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("phase", []),
        ("failures", "3"),
        ("failure_kind", {}),
        ("last_outage_result", []),
    ],
)
async def test_corrupt_incident_is_skipped_and_rebuilds_baseline(field, value):
    context, kv, _, _, monitor = services(probe=Observations())
    state = await monitor.get_state(context.robots[0])
    state[field] = value
    kv.data["connection_incident:native-1"] = state
    monitor = services(context, kv, Observations())[-1]
    fresh = await monitor.get_state(context.robots[0])
    assert fresh["phase"] == "UNKNOWN" and fresh["failures"] == 0


async def test_slow_bot_does_not_block_other_bot_and_disable_during_probe_prevents_send():
    release = asyncio.Event()
    fast_checked = asyncio.Event()

    async def probe(bot):
        if bot["id"] == "native-1":
            await release.wait()
        else:
            fast_checked.set()
        return module("connection_probe").ProbeResult("offline")

    context, _, bindings, _, monitor = services(probe=probe)
    context.robots.append(robot(id="native-2"))
    await bindings.execute("bind", chat_event(), "native-1")
    task = asyncio.create_task(monitor.check_all())
    try:
        await asyncio.wait_for(fast_checked.wait(), 1)
        context.robots[0]["enable"] = False
        release.set()
        await task
        assert context.sent == []
    finally:
        release.set()
        await task


async def test_plugin_lifecycle_owns_monitor_task():
    from test_connection_view import plugin

    instance, context = plugin([])
    await instance.initialize()
    task = instance.connection_monitor.task
    await instance.initialize()
    assert instance.connection_monitor.task is task
    await instance.terminate()
    assert task.done() and instance.connection_monitor.task is None


async def test_concurrent_status_read_cannot_restore_pre_incident_cache_and_duplicate_alert():
    probe = Observations()
    context, kv, bindings, _, monitor = services(probe=probe)
    await bindings.execute("bind", chat_event())
    await ticks(monitor, 2)
    monitor = services(context, kv, probe)[-1]
    captured, release = asyncio.Event(), asyncio.Event()
    original_get = kv.get_kv_data

    async def delayed_get(key, default=None):
        value = await original_get(key, default)
        if (
            key.startswith("connection_incident:")
            and asyncio.current_task().get_name() == "status-read"
        ):
            captured.set()
            await release.wait()
        return value

    kv.get_kv_data = delayed_get
    status_task = asyncio.create_task(monitor.get_state(context.robots[0]), name="status-read")
    try:
        await asyncio.wait_for(captured.wait(), 1)
        await ticks(monitor, 1)
        assert len(context.sent) == 1
        release.set()
        await status_task
        await ticks(monitor, 1)
        assert len(context.sent) == 1
    finally:
        release.set()
        await status_task
