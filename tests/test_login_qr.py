import pytest
from conftest import module


class _Client:
    def __init__(self, operations, qr=b"\x89PNG\r\n\x1a\n" + b"0" * 32):
        self._operations = list(operations)
        self._qr = qr
        self.requested = None

    async def start_login(self):
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
    path = await login_qr.create_login_qr(client, tmp_path)

    assert path.read_bytes().startswith(b"\x89PNG")
    assert client.requested == ("op-1", 2)


@pytest.mark.asyncio
async def test_terminal_state_raises(tmp_path):
    login_qr = module("login_qr")
    client = _Client([{"id": "op-1", "state": "failed", "result": {}}])
    with pytest.raises(login_qr.LoginQRUnavailable):
        await login_qr.create_login_qr(client, tmp_path)
