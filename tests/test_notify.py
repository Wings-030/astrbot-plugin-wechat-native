import json
import logging
import types

import pytest
from conftest import module

LOG = logging.getLogger("notify-test")


class _Meta:
    def __init__(self, name, id):
        self.name = name
        self.id = id


class _FakeContext:
    def __init__(self, instances=None):
        self.platform_manager = types.SimpleNamespace(platform_insts=instances or [])
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(umo)
        return True


class _FakeClient:
    async def start_login(self):
        return {"id": "op-1", "state": "starting", "result": {}}

    async def operation(self, operation_id):
        return {
            "id": operation_id,
            "state": "awaiting_scan",
            "result": {"qrAvailable": True, "qrGeneration": 1},
        }

    async def operation_qr(self, operation_id, generation):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 64


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
        self._context_tokens = users or {"user@im.wechat": "token"}
        self._recent_messages = {}

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
async def test_offline_alert_threshold_and_once(tmp_path):
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
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1


@pytest.mark.asyncio
async def test_recovery_notification(tmp_path):
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
    await manager._apply(adapter, runtime, False, "disconnected")
    await manager._apply(adapter, runtime, True, "online")
    assert len(context.sent) == 2


@pytest.mark.asyncio
async def test_auto_discovers_and_persists_target(tmp_path):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(_config(config_mod.NativeConfig))
    context = _FakeContext([_FakeWxoc()])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]

    saved = json.loads((tmp_path / notify.STATE_FILE).read_text(encoding="utf-8"))
    assert saved["umo"] == "weixin_personal_wpic:FriendMessage:user@im.wechat"

    reloaded = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    assert reloaded._state["umo"] == saved["umo"]


@pytest.mark.asyncio
async def test_ambiguous_target_is_not_guessed(tmp_path):
    notify = module("notify")
    config_mod = module("config")
    adapter = _FakeAdapter(_config(config_mod.NativeConfig))
    wxoc = _FakeWxoc(users={"u1@im.wechat": "t1", "u2@im.wechat": "t2"})
    context = _FakeContext([wxoc])
    manager = notify.OfflineNotifyManager(context, LOG, state_dir=tmp_path)
    runtime = manager._runtime_from_state("wechat_native")

    await manager._apply(adapter, runtime, False, "disconnected")
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
async def test_email_channel_alone(tmp_path, monkeypatch):
    notify = module("notify")
    config_mod = module("config")
    _FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)
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
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["To"] == "me@example.com"
    assert "掉线" in _FakeSMTP.sent[0]["Subject"]
    assert context.sent == []

    await manager._apply(adapter, runtime, True, "online")
    assert len(_FakeSMTP.sent) == 2
    assert "恢复" in _FakeSMTP.sent[1]["Subject"]


@pytest.mark.asyncio
async def test_both_channels_send_together(tmp_path, monkeypatch):
    notify = module("notify")
    config_mod = module("config")
    _FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)
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
    await manager._apply(adapter, runtime, False, "disconnected")
    assert context.sent == ["weixin_personal_wpic:FriendMessage:user@im.wechat"]
    assert len(_FakeSMTP.sent) == 1


@pytest.mark.asyncio
async def test_email_failure_does_not_block_wechat(tmp_path, monkeypatch):
    notify = module("notify")
    config_mod = module("config")

    class _BrokenSMTP(_FakeSMTP):
        def __init__(self, host, port, timeout=None):
            raise OSError("smtp down")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _BrokenSMTP)
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
    await manager._apply(adapter, runtime, False, "disconnected")
    assert len(context.sent) == 1


@pytest.mark.asyncio
async def test_login_qr_sent_after_offline_alert(tmp_path):
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
    await manager._apply(adapter, runtime, False, "authenticated=False", needs_login=True)
    assert len(context.sent) == 2
    assert (tmp_path / "login-qr.png").exists()


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
