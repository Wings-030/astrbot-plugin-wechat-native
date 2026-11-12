import asyncio
import json
import logging
import os
import stat
import time
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from conftest import module
from test_native_client import VALID_PNG

LOG = logging.getLogger("notify-test")


@pytest.fixture
def clock(monkeypatch):
    current = types.SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        module("notify"),
        "time",
        types.SimpleNamespace(
            time=lambda: current.now,
            monotonic=lambda: current.now,
            strftime=time.strftime,
            localtime=time.localtime,
        ),
    )
    return current


class _Meta:
    def __init__(self, name, id):
        self.name = name
        self.id = id


class _FakeContext:
    def __init__(self, instances=None):
        self.platform_manager = types.SimpleNamespace(platform_insts=instances or [])
        self.sent = []
        self.chains = []
        self.database = types.SimpleNamespace(
            get_umo_aliases=AsyncMock(
                return_value=[
                    types.SimpleNamespace(umo=f"{instance.meta().id}:FriendMessage:{user}")
                    for instance in instances or []
                    if hasattr(instance, "sessions")
                    for user in instance.sessions
                ]
            ),
            get_filtered_conversations=AsyncMock(
                return_value=(
                    [
                        types.SimpleNamespace(
                            user_id=f"{instance.meta().id}:FriendMessage:{user}",
                            updated_at=datetime.fromtimestamp(updated, UTC),
                        )
                        for instance in instances or []
                        if hasattr(instance, "sessions")
                        for user, updated in instance.sessions.items()
                        if updated
                    ],
                    0,
                )
            ),
        )

    def get_db(self):
        return self.database

    async def send_message(self, umo, chain):
        self.sent.append(umo)
        self.chains.append(chain)
        return True


class _FakeClient:
    async def start_login(self, **kwargs):
        return {"id": "op-1", "state": "starting", "result": {}}

    async def operation(self, operation_id):
        return {
            "id": operation_id,
            "state": "awaiting_scan",
            "result": {"qrAvailable": True, "qrGeneration": 1},
        }

    async def operation_qr(self, operation_id, generation):
        return VALID_PNG


class _FakeAdapter:
    def __init__(self, config, platform_id="wechat_native", client=None):
        self.native_config = config
        self.stopped = False
        self._platform_id = platform_id
        self.client = client

    def meta(self):
        return _Meta("wechat_native", self._platform_id)


class _FakeWxoc:
    def __init__(self, platform_id="weixin_personal_wpic", users=None):
        self._platform_id = platform_id
        self.sessions = dict.fromkeys(users or ["user@im.wechat"], 0)

    def meta(self):
        return _Meta("weixin_oc", self._platform_id)


def _config(NotifyConfig, **overrides):
    data = {
        "server_url": "http://127.0.0.1:6174",
        "account_id": "account-1",
        "token": "t",
        "enable_offline_notify": True,
    }
    data.update(overrides)
    return NotifyConfig.from_dict(data)


@pytest.mark.asyncio
async def test_offline_alert_threshold_and_once(tmp_path, clock):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            offline_notify_umo="weixin_personal_wpic:FriendMessage:user@im.wechat",
            offline_notify_after_seconds=60,
            offline_notify_repeat_hours=0,
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == []
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1


@pytest.mark.asyncio
async def test_recovery_notification(tmp_path, clock):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            offline_notify_umo="weixin_personal_wpic:FriendMessage:user@im.wechat",
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    await manager._apply(adapter, runtime, True, "online")
    assert len(context.sent) == 2


@pytest.mark.asyncio
async def test_auto_discovers_and_persists_target(tmp_path, clock):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(_config(config_mod.NativeConfig))
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]

    saved = json.loads((tmp_path / notify.STATE_FILE).read_text(encoding="utf-8"))
    assert saved["umo"] == "weixin_personal_wpic:FriendMessage:user@im.wechat"

    reloaded = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    assert reloaded._state["umo"] == saved["umo"]


@pytest.mark.asyncio
async def test_ambiguous_target_is_not_guessed(tmp_path, clock):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(_config(config_mod.NativeConfig))
    wxoc = _FakeWxoc(users={"u1@im.wechat": "t1", "u2@im.wechat": "t2"})
    context = _FakeContext([wxoc])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == []


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        type(self).sent.append(message)


@pytest.mark.asyncio
async def test_email_channel_alone(tmp_path, monkeypatch, clock):
    notify = module("notify")
    config_mod = module("config")
    _FakeSMTP.sent = []
    monkeypatch.setattr(module("email_notify").smtplib, "SMTP_SSL", _FakeSMTP)
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            enable_offline_notify=False,
            enable_email_notify=True,
            email_smtp_host="smtp.example.com",
            email_smtp_user="bot@example.com",
            email_smtp_password="secret",
            email_to="me@example.com",
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["To"] == "me@example.com"
    assert "掉线" in _FakeSMTP.sent[0]["Subject"]
    assert context.sent == []

    await manager._apply(adapter, runtime, True, "online")
    assert len(_FakeSMTP.sent) == 2
    assert "恢复" in _FakeSMTP.sent[1]["Subject"]


@pytest.mark.asyncio
async def test_both_channels_send_together(tmp_path, monkeypatch, clock):
    notify = module("notify")
    config_mod = module("config")
    _FakeSMTP.sent = []
    monkeypatch.setattr(module("email_notify").smtplib, "SMTP_SSL", _FakeSMTP)
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            offline_notify_umo="weixin_personal_wpic:FriendMessage:user@im.wechat",
            enable_email_notify=True,
            email_smtp_host="smtp.example.com",
            email_smtp_user="bot@example.com",
            email_smtp_password="secret",
            email_to="me@example.com",
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]
    assert len(_FakeSMTP.sent) == 1


@pytest.mark.asyncio
async def test_email_failure_does_not_block_wechat(tmp_path, monkeypatch, clock):
    notify = module("notify")
    config_mod = module("config")

    class _BrokenSMTP(_FakeSMTP):
        def __init__(self, host, port, timeout=None):
            raise OSError("smtp down")

    monkeypatch.setattr(module("email_notify").smtplib, "SMTP_SSL", _BrokenSMTP)
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            offline_notify_umo="weixin_personal_wpic:FriendMessage:user@im.wechat",
            enable_email_notify=True,
            email_smtp_host="smtp.example.com",
            email_to="me@example.com",
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 60
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1


@pytest.mark.asyncio
async def test_login_qr_sent_after_offline_alert(tmp_path, clock):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(
        _config(
            config_mod.NativeConfig,
            offline_notify_umo="weixin_personal_wpic:FriendMessage:user@im.wechat",
            enable_login_qr=True,
        ),
        client=_FakeClient(),
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "authenticated=False", needs_login=True)
    clock.now += 60
    await manager._apply(adapter, runtime, False, "authenticated=False", needs_login=True)
    assert len(context.sent) == 2
    assert not list(tmp_path.glob("*.png"))


@pytest.mark.parametrize(
    "change",
    [
        {"offline_notify_after_seconds": 5},
        {"offline_notify_after_seconds": 3601},
        {"offline_notify_repeat_hours": -1},
        {"offline_notify_repeat_hours": 169},
        {"offline_notify_umo": 5},
        {"enable_offline_notify": "true"},
        {"offline_notify_on_recovery": "true"},
        {"email_smtp_port": 0},
        {"email_smtp_port": 70000},
        {"email_smtp_security": "tls"},
        {"email_to": 1},
        {"email_smtp_password_file": 1},
        {"enable_email_notify": "yes"},
        {"enable_login_qr": "true"},
    ],
)
def test_notify_config_validation(change):
    config_mod = module("config")
    data = {
        "server_url": "http://127.0.0.1:6174",
        "account_id": "account-1",
        "token": "t",
    }
    data.update(change)
    with pytest.raises(ValueError):
        config_mod.NativeConfig.from_dict(data)


@pytest.mark.parametrize("threshold", [10, 30, 60])
async def test_threshold_uses_elapsed_time(tmp_path, clock, threshold):
    notify = module("notify")
    adapter = _FakeAdapter(
        _config(
            module("config").NativeConfig,
            offline_notify_after_seconds=threshold,
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = notify._Runtime()
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == []
    clock.now += threshold - 1
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == []
    clock.now += 1
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1


async def test_transient_failure_resets_timer_without_recovery_alert(tmp_path, clock):
    notify = module("notify")
    adapter = _FakeAdapter(_config(module("config").NativeConfig))
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = notify._Runtime()
    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 30
    await manager._apply(adapter, runtime, True, "online")
    assert runtime.down_since is None
    await manager._apply(adapter, runtime, False, "disconnected")
    clock.now += 59
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == []


async def test_pending_outage_and_duration_survive_restart(tmp_path, clock):
    notify = module("notify")
    adapter = _FakeAdapter(
        _config(
            module("config").NativeConfig,
            offline_notify_repeat_hours=1,
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime["wechat_native"] = notify._Runtime()
    await manager._apply(adapter, runtime, False, "disconnected")
    manager._save_state()
    clock.now += 60
    reloaded = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = reloaded._runtime["wechat_native"] = reloaded._runtime_from_state("wechat_native")
    await reloaded._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1
    assert runtime.down_since == 1000.0
    reloaded._save_state()
    clock.now += 3600
    restarted = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = restarted._runtime_from_state("wechat_native")
    await restarted._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 2
    assert "持续：1 小时 1 分钟" in context.chains[-1].chain[0].text


@pytest.mark.parametrize("first_needs_login", [False, True])
async def test_qr_is_retried_without_repeating_once_only_alert(
    tmp_path,
    clock,
    first_needs_login,
):
    notify = module("notify")
    adapter = _FakeAdapter(
        _config(
            module("config").NativeConfig,
            enable_login_qr=True,
            offline_notify_repeat_hours=0,
        )
    )
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    manager._send_login_qr = AsyncMock(side_effect=[False, True])
    runtime = notify._Runtime(down_since=clock.now - 60)
    await manager._apply(adapter, runtime, False, "disconnected", first_needs_login)
    for _ in range(3):
        clock.now += 30
        await manager._apply(adapter, runtime, False, "unauthenticated", True)
    assert len(context.sent) == 1
    assert manager._send_login_qr.await_count == 2
    assert runtime.qr_sent


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
async def test_automatic_qr_cleanup_on_send_failure(tmp_path, monkeypatch, error):
    notify = module("notify")
    monkeypatch.setattr(notify, "create_login_qr", AsyncMock(return_value=VALID_PNG))
    adapter = _FakeAdapter(
        _config(
            module("config").NativeConfig,
            enable_login_qr=True,
            offline_notify_umo="wxoc:FriendMessage:owner",
        )
    )
    context = _FakeContext()

    async def send(_, chain):
        assert chain.chain[-1].path
        assert list(tmp_path.glob("*.png"))
        raise error("sensitive detail")

    context.send_message = send
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    if error is asyncio.CancelledError:
        with pytest.raises(error):
            await manager._send_login_qr(adapter)
    else:
        assert await manager._send_login_qr(adapter) is False
    assert not list(tmp_path.glob("*.png"))


def test_state_file_is_private_and_qr_delivery_is_persisted(tmp_path):
    notify = module("notify")
    manager = notify.OfflineNotifyManager(_FakeContext(), LOG, state_dir=tmp_path)
    manager._runtime["wechat_native"] = notify._Runtime(
        online=False,
        down_since=1000,
        last_alert_ts=1060,
        qr_sent=True,
    )
    manager._save_state()
    if os.name == "posix":
        assert stat.S_IMODE(manager.state_path.stat().st_mode) == 0o600
    reloaded = notify.OfflineNotifyManager(_FakeContext(), LOG, state_dir=tmp_path)
    runtime = reloaded._runtime_from_state("wechat_native")
    assert runtime.qr_sent
    assert runtime.down_since == 1000
    assert not list(tmp_path.glob("*.tmp"))


def test_state_write_failure_logs_only_error_type(tmp_path, monkeypatch):
    notify = module("notify")
    logger = Mock()
    manager = notify.OfflineNotifyManager(_FakeContext(), logger, state_dir=tmp_path)
    monkeypatch.setattr(notify.tempfile, "mkstemp", Mock(side_effect=OSError("secret URL")))
    manager._save_state()
    logger.warning.assert_called_once_with("掉线提醒状态写入失败：%s", "OSError")


async def test_watcher_failure_logs_only_error_type(tmp_path, monkeypatch):
    notify = module("notify")
    logger = Mock()
    manager = notify.OfflineNotifyManager(_FakeContext(), logger, state_dir=tmp_path)
    manager.poll_once = AsyncMock(side_effect=OSError("secret URL"))
    monkeypatch.setattr(
        notify.asyncio,
        "sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    with pytest.raises(asyncio.CancelledError):
        await manager._run()
    logger.warning.assert_called_once_with("掉线提醒巡检异常：%s", "OSError")


def test_stopped_adapters_are_ignored_in_both_entry_points(tmp_path):
    config = _config(module("config").NativeConfig)
    stopped, active = _FakeAdapter(config), _FakeAdapter(config, "active")
    stopped.stopped = True
    context = _FakeContext([stopped, active])
    manager = module("notify").OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    assert manager._adapters() == [active]
    assert (
        module("main").WeChatNativePlugin._native_adapter(
            types.SimpleNamespace(
                context=context,
            )
        )
        is active
    )


async def test_discovery_uses_latest_active_session():
    targets = module("notify_targets")
    stopped = _FakeWxoc("stopped")
    stopped.stopped = True
    stopped.sessions = {"user@im.wechat": 500}
    active = _FakeWxoc(users={"older": "token", "newer": "token"})
    active.sessions = {"older": 100, "newer": 200}
    context = _FakeContext([stopped, active])
    assert await targets.best_weixin_umo(context) == ("weixin_personal_wpic:FriendMessage:newer")
    assert context.database.get_filtered_conversations.call_args.kwargs["include_history"] is False


@pytest.mark.parametrize("security", ["ssl", "starttls", "none"])
def test_smtp_password_file_takes_precedence(tmp_path, monkeypatch, security):
    email = module("email_notify")
    secret = tmp_path / "smtp-password"
    secret.write_text("file-secret\n", encoding="utf-8")
    server = Mock()
    transport = Mock()
    transport.return_value.__enter__ = Mock(return_value=server)
    transport.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(email.smtplib, "SMTP_SSL" if security == "ssl" else "SMTP", transport)
    config = _config(
        module("config").NativeConfig,
        email_smtp_host="smtp.example.com",
        email_smtp_user="bot@example.com",
        email_smtp_password="inline-secret",
        email_smtp_password_file=str(secret),
        email_smtp_security=security,
        email_to="owner@example.com",
    )
    email.send_email(config.email, "subject", "text")
    server.login.assert_called_once_with("bot@example.com", "file-secret")
    assert server.starttls.call_count == (1 if security == "starttls" else 0)
    server.send_message.assert_called_once()
    assert "file-secret" not in repr(config)
    assert "inline-secret" not in repr(config)


@pytest.mark.parametrize("contents", ["", "x" * 8193, "secret\nsecond-line", "\x00"])
def test_invalid_smtp_password_file_is_sanitized(tmp_path, contents):
    path = tmp_path / "smtp-password"
    path.write_text(contents, encoding="utf-8")
    config = _config(module("config").NativeConfig, email_smtp_password_file=str(path))
    with pytest.raises(ValueError, match="必须包含有效授权码"):
        config.email.load_password()


def test_missing_smtp_password_file_is_sanitized(tmp_path):
    config = _config(
        module("config").NativeConfig,
        email_smtp_password_file=str(tmp_path / "missing"),
        email_smtp_password="inline-secret",
    )
    with pytest.raises(ValueError, match="不可读") as error:
        config.email.load_password()
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("restart", [False, True])
async def test_qr_delivery_retry_reuses_active_native_operation(
    serve,
    tmp_path,
    clock,
    restart,
):
    notify = module("notify")
    native = module("native_client")
    started = []
    image_attempts = []
    operation = {
        "id": "active-login",
        "state": "awaiting_scan",
        "result": {"qrGeneration": 1, "qrAvailable": True},
    }

    async def handler(request):
        if request.path.endswith("/auth/login"):
            if started:
                return web.json_response({"error": {"code": "operation_in_progress"}}, status=409)
            started.append(request.headers["Idempotency-Key"])
            return web.json_response(operation)
        if request.path.endswith("/qr"):
            return web.Response(body=VALID_PNG, content_type="image/png")
        if "/operations/" in request.path:
            return web.json_response(operation)
        return web.json_response({"authenticated": False, "connection": "offline"})

    context = _FakeContext()
    original_send = context.send_message

    async def send(target, chain):
        if len(chain.chain) > 1:
            image_attempts.append(chain.chain[-1].path)
            if len(image_attempts) == 1:
                raise OSError("temporary send failure")
        return await original_send(target, chain)

    context.send_message = send
    settings = _config(
        module("config").NativeConfig,
        enable_login_qr=True,
        offline_notify_repeat_hours=0,
        offline_notify_umo="wxoc:FriendMessage:owner",
    )
    async with native.NativeClient(await serve(handler) + "/v1", "a", "test-token") as client:
        adapter = _FakeAdapter(settings, client=client)
        context.platform_manager.platform_insts = [adapter]
        manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
        manager._runtime["wechat_native"] = notify._Runtime(down_since=clock.now - 60)
        await manager.poll_once()
        if restart:
            manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
        clock.now += 30
        await manager.poll_once()
        clock.now += 30
        await manager.poll_once()
    assert len(started) == 1
    assert len(image_attempts) == 2
    assert manager._runtime["wechat_native"].qr_sent
    assert len(context.sent) == 2
    assert not list(tmp_path.glob("*.png"))


async def test_lost_login_response_reuses_idempotency_key_after_restart(serve, tmp_path):
    notify = module("notify")
    native = module("native_client")
    keys = []
    operation = {
        "id": "accepted-login",
        "state": "awaiting_scan",
        "result": {"qrGeneration": 1, "qrAvailable": True},
    }

    async def handler(request):
        if request.path.endswith("/auth/login"):
            key = request.headers["Idempotency-Key"]
            keys.append(key)
            if len(keys) == 1:
                return web.json_response({"error": {"code": "timeout"}}, status=504)
            if key != keys[0]:
                return web.json_response({"error": {"code": "operation_in_progress"}}, status=409)
            return web.json_response(operation)
        return web.Response(body=VALID_PNG, content_type="image/png")

    context = _FakeContext()
    settings = _config(module("config").NativeConfig)
    async with native.NativeClient(await serve(handler) + "/v1", "a", "test-token") as client:
        adapter = _FakeAdapter(settings, client=client)
        manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
        with pytest.raises(native.NativeAPIError):
            await manager.get_login_qr(adapter)
        reloaded = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
        assert await reloaded.get_login_qr(adapter) == VALID_PNG
    assert len(keys) == 2
    assert keys[0] == keys[1]


@pytest.mark.parametrize("platform,private", [("wechat_native", True), ("weixin_oc", False)])
def test_session_observer_ignores_other_sources(tmp_path, platform, private):
    manager = module("notify").OfflineNotifyManager(_FakeContext(), LOG, state_dir=tmp_path)
    event = types.SimpleNamespace(
        get_platform_name=lambda: platform,
        is_private_chat=lambda: private,
        unified_msg_origin="unrelated:GroupMessage:room",
    )
    manager.observe_session(event)
    assert not manager._state.get("umo")
    assert not manager.state_path.exists()


async def test_public_message_hook_updates_persisted_target(tmp_path):
    main = module("main")
    notify = module("notify")
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    plugin = types.SimpleNamespace(_offline_notify=manager)
    event = types.SimpleNamespace(
        get_platform_name=lambda: "weixin_oc",
        is_private_chat=lambda: True,
        unified_msg_origin="weixin_personal_wpic:FriendMessage:latest",
    )
    await main.WeChatNativePlugin.remember_notification_session(plugin, event)
    reloaded = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    adapter = _FakeAdapter(_config(module("config").NativeConfig))
    assert await reloaded._resolve_umo(adapter) == event.unified_msg_origin
    context.database.get_umo_aliases.assert_not_awaited()


async def test_ambiguous_metadata_timestamps_are_not_guessed():
    active = _FakeWxoc(users=["one", "two"])
    active.sessions = {"one": 100, "two": 100}
    assert await module("notify_targets").best_weixin_umo(_FakeContext([active])) is None


async def test_discovery_with_supported_astrbot_database(tmp_path):
    from astrbot.core.db.sqlite import SQLiteDatabase

    database = SQLiteDatabase(str(tmp_path / "sessions.sqlite3"))
    try:
        await database.initialize()
        platform_id = "weixin_personal_wpic"
        for user, updated in [("older", 100), ("newer", 200)]:
            umo = f"{platform_id}:FriendMessage:{user}"
            await database.upsert_umo_alias(umo, user, user, None)
            await database.create_conversation(
                user_id=umo, platform_id=platform_id,
                updated_at=datetime.fromtimestamp(updated, UTC),
                content=[{"role": "user", "content": "history must stay unloaded"}],
            )
        await database.create_conversation(
            user_id=f"{platform_id}:GroupMessage:room", platform_id=platform_id,
            updated_at=datetime.fromtimestamp(300, UTC),
        )
        context = _FakeContext([_FakeWxoc()])
        context.database = database
        assert await module("notify_targets").best_weixin_umo(context) == (
            f"{platform_id}:FriendMessage:newer"
        )
    finally:
        await database.engine.dispose()


async def test_metadata_discovery_excludes_groups_and_inactive_platforms():
    context = _FakeContext([_FakeWxoc()])
    context.database.get_umo_aliases.return_value = [
        types.SimpleNamespace(umo="weixin_personal_wpic:GroupMessage:room"),
        types.SimpleNamespace(umo="removed:FriendMessage:owner"),
    ]
    assert await module("notify_targets").best_weixin_umo(context) is None


async def test_session_discovery_error_does_not_block_email(tmp_path, monkeypatch):
    email = module("email_notify")
    _FakeSMTP.sent = []
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", _FakeSMTP)
    context = _FakeContext([_FakeWxoc()])
    context.database.get_umo_aliases.side_effect = OSError("private details")
    adapter = _FakeAdapter(
        _config(
            module("config").NativeConfig,
            enable_email_notify=True,
            email_smtp_host="smtp.example.com",
            email_to="owner@example.com",
        )
    )
    manager = module("notify").OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    assert await manager._send(adapter, "subject", "body")
    assert len(_FakeSMTP.sent) == 1


def test_atomic_state_write_failure_preserves_previous_state(tmp_path, monkeypatch):
    from pathlib import Path

    manager = module("notify").OfflineNotifyManager(_FakeContext(), LOG, state_dir=tmp_path)
    manager._save_state()
    before = manager.state_path.read_bytes()
    manager._state["umo"] = "wxoc:FriendMessage:owner"
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("sensitive details")))
    manager._save_state()
    assert manager.state_path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
