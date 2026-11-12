import asyncio
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import module
from test_native_client import VALID_PNG


class _Client:
    def __init__(self, operations, qr=VALID_PNG):
        self._operations = list(operations)
        self._qr = qr
        self.requested = None

    async def start_login(self, **kwargs):
        return {"id": "op-1", "state": "starting", "result": {}}

    async def operation(self, operation_id):
        assert operation_id == "op-1"
        return self._operations.pop(0) if len(self._operations) > 1 else self._operations[0]

    async def operation_qr(self, operation_id, generation):
        self.requested = (operation_id, generation)
        return self._qr


@pytest.mark.asyncio
async def test_fetches_qr_after_polling(tmp_path, monkeypatch):
    login_qr = module("login_qr")
    client = _Client(
        [
            {"id": "op-1", "state": "starting", "result": {}},
            {
                "id": "op-1",
                "state": "awaiting_scan",
                "result": {"qrAvailable": True, "qrGeneration": 2},
            },
        ]
    )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(login_qr.asyncio, "sleep", _no_sleep)
    data = await login_qr.create_login_qr(client)

    assert data == VALID_PNG
    assert list(tmp_path.iterdir()) == []
    assert client.requested == ("op-1", 2)


@pytest.mark.asyncio
async def test_terminal_state_raises(tmp_path):
    login_qr = module("login_qr")
    client = _Client([{"id": "op-1", "state": "failed", "result": {}}])
    with pytest.raises(login_qr.LoginQRUnavailable):
        await login_qr.create_login_qr(client)


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\ntruncated", VALID_PNG[:-12], b"<html>"])
async def test_rejects_invalid_images(data):
    login_qr = module("login_qr")
    client = _Client(
        [{"state": "awaiting_scan", "result": {"qrAvailable": True, "qrGeneration": 1}}],
        qr=data,
    )
    with pytest.raises(login_qr.LoginQRUnavailable, match="invalid_qr_image"):
        await login_qr.create_login_qr(client)


def test_qr_files_are_private_unique_and_removed(tmp_path):
    login_qr = module("login_qr")
    with login_qr.login_qr_file(VALID_PNG, tmp_path) as first:
        with login_qr.login_qr_file(VALID_PNG, tmp_path) as second:
            assert first != second
            assert first.read_bytes() == second.read_bytes() == VALID_PNG
            if os.name == "posix":
                assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert not second.exists()
        assert first.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_qr_file_cleanup_on_failure_and_cancellation(tmp_path, error):
    with pytest.raises(error), module("login_qr").login_qr_file(VALID_PNG, tmp_path) as path:
        assert path.exists()
        raise error()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "platform,private", [("weixin_oc", False), ("wechat_native", True), ("other", True)]
)
async def test_command_ignores_groups_and_other_platforms(platform, private):
    main = module("main")

    def unexpected_lookup():
        pytest.fail("ignored command must not look up an account or generate a QR")

    plugin = SimpleNamespace(_native_adapter=unexpected_lookup)
    event = SimpleNamespace(
        get_platform_name=lambda: platform,
        is_private_chat=lambda: private,
    )
    assert [
        result async for result in main.WeChatNativePlugin.send_wechat_login_qr(plugin, event)
    ] == []


@pytest.mark.parametrize("error", [None, OSError, asyncio.CancelledError])
async def test_command_keeps_file_until_send_finishes(tmp_path, monkeypatch, error):
    main = module("main")
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _: tmp_path)

    async def account():
        return {"authenticated": True, "connection": "online"}

    async def qr(_):
        return VALID_PNG

    plugin = SimpleNamespace(
        _native_adapter=lambda: SimpleNamespace(client=SimpleNamespace(account=account)),
        logger=logging.getLogger("qr-command-test"),
        _offline_notify=SimpleNamespace(get_login_qr=qr),
    )
    paths = []

    async def send(chain):
        image = chain.chain[-1]
        path = Path(await image.convert_to_file_path())
        assert await asyncio.to_thread(path.read_bytes) == VALID_PNG
        paths.append(path)
        if error:
            raise error()

    event = SimpleNamespace(
        get_platform_name=lambda: "weixin_oc",
        is_private_chat=lambda: True,
        send=send,
    )
    if error:
        with pytest.raises(error):
            async for _ in main.WeChatNativePlugin.send_wechat_login_qr(plugin, event):
                pytest.fail("QR delivery must complete inside the command")
    else:
        assert [
            result async for result in main.WeChatNativePlugin.send_wechat_login_qr(plugin, event)
        ] == []
    assert len(paths) == 1
    assert not paths[0].exists()
    assert list(tmp_path.iterdir()) == []


async def test_login_request_timeout_retries_with_the_same_idempotency_key():
    qr = module("login_qr")
    api = module("native_client")
    session = qr.LoginQRSession()
    client = SimpleNamespace(
        start_login=AsyncMock(
            side_effect=[
                api.NativeAPIError(0, "timeout"),
                {
                    "id": "op-1",
                    "state": "awaiting_scan",
                    "result": {"qrAvailable": True, "qrGeneration": 1},
                },
            ]
        ),
        operation_qr=AsyncMock(return_value=VALID_PNG),
    )
    with pytest.raises(api.NativeAPIError):
        await qr.create_login_qr(client, session=session)
    assert await qr.create_login_qr(client, session=session) == VALID_PNG
    first, second = client.start_login.call_args_list
    assert first.kwargs["idempotency_key"] == second.kwargs["idempotency_key"]


async def test_qr_poll_timeout_retains_the_active_operation(monkeypatch):
    qr = module("login_qr")
    session = qr.LoginQRSession()
    client = _Client(
        [
            {
                "id": "op-1",
                "state": "awaiting_scan",
                "result": {"qrAvailable": True, "qrGeneration": 1},
            }
        ]
    )
    client.start_login = AsyncMock(wraps=client.start_login)
    monkeypatch.setattr(qr, "QR_WAIT_SECONDS", 0)
    with pytest.raises(qr.LoginQRUnavailable, match="qr_timeout"):
        await qr.create_login_qr(client, session=session)
    assert session.operation_id == "op-1"
    assert await qr.create_login_qr(client, session=session) == VALID_PNG
    assert client.start_login.await_count == 1


@pytest.mark.parametrize("state", ["failed", "expired", "succeeded", "cancelled"])
async def test_terminal_login_operations_are_replaced(state):
    qr = module("login_qr")
    session = qr.LoginQRSession(operation_id="old-operation")
    previous_key = session.request_key
    client = SimpleNamespace(
        operation=AsyncMock(return_value={"state": state}),
        start_login=AsyncMock(
            return_value={
                "id": "new-operation",
                "state": "awaiting_scan",
                "result": {"qrAvailable": True, "qrGeneration": 1},
            }
        ),
        operation_qr=AsyncMock(return_value=VALID_PNG),
    )
    assert await qr.create_login_qr(client, session=session) == VALID_PNG
    assert session.operation_id == "new-operation"
    assert session.request_key != previous_key


async def test_disappeared_login_operation_is_replaced():
    qr = module("login_qr")
    client = SimpleNamespace(
        operation=AsyncMock(side_effect=module("native_client").NativeAPIError(404)),
        start_login=AsyncMock(
            return_value={
                "id": "new-operation",
                "state": "awaiting_scan",
                "result": {"qrAvailable": True, "qrGeneration": 1},
            }
        ),
        operation_qr=AsyncMock(return_value=VALID_PNG),
    )
    session = qr.LoginQRSession(operation_id="removed-operation")
    assert await qr.create_login_qr(client, session=session) == VALID_PNG
    assert session.operation_id == "new-operation"


async def test_concurrent_qr_requests_share_one_login_operation():
    qr = module("login_qr")
    operation = {
        "id": "op-1",
        "state": "awaiting_scan",
        "result": {"qrAvailable": True, "qrGeneration": 1},
    }

    async def start(**kwargs):
        await asyncio.sleep(0)
        return operation

    client = SimpleNamespace(
        start_login=AsyncMock(side_effect=start),
        operation=AsyncMock(return_value=operation),
        operation_qr=AsyncMock(return_value=VALID_PNG),
    )
    session = qr.LoginQRSession()
    assert await asyncio.gather(
        qr.create_login_qr(client, session=session),
        qr.create_login_qr(client, session=session),
    ) == [VALID_PNG, VALID_PNG]
    assert client.start_login.await_count == 1


async def test_terminal_state_rejects_a_stale_available_qr():
    qr = module("login_qr")
    client = SimpleNamespace(
        start_login=AsyncMock(
            return_value={
                "id": "op-1",
                "state": "expired",
                "result": {"qrAvailable": True, "qrGeneration": 1},
            }
        ),
        operation_qr=AsyncMock(return_value=VALID_PNG),
    )
    with pytest.raises(qr.LoginQRUnavailable, match="expired"):
        await qr.create_login_qr(client)
    client.operation_qr.assert_not_awaited()
