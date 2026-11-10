"""Fetch a WeChat login QR through the Native HTTP API.

The daemon exposes the login operation and its QR image directly, so this stays
portable: no Docker access, CLI invocation, or host-specific paths are needed.
"""

import asyncio
from pathlib import Path

QR_WAIT_SECONDS = 20
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "canceled", "expired", "logged_in"}


class LoginQRUnavailable(RuntimeError):
    """The daemon could not produce a scannable QR right now."""


async def create_login_qr(client, directory: Path) -> Path:
    operation = await client.start_login()
    operation_id = operation.get("id")
    if not isinstance(operation_id, str) or not operation_id:
        raise LoginQRUnavailable("login_operation_missing")
    deadline = asyncio.get_running_loop().time() + QR_WAIT_SECONDS
    generation = 0
    while True:
        result = operation.get("result") or {}
        generation = int(result.get("qrGeneration") or 0)
        if generation > 0 and result.get("qrAvailable"):
            break
        state = str(operation.get("state") or "")
        if state in TERMINAL_STATES:
            raise LoginQRUnavailable(state or "login_failed")
        if asyncio.get_running_loop().time() >= deadline:
            raise LoginQRUnavailable("qr_timeout")
        await asyncio.sleep(1)
        operation = await client.operation(operation_id)
    data = await client.operation_qr(operation_id, generation)
    if not data.startswith(b"\x89PNG"):
        raise LoginQRUnavailable("invalid_qr_image")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    path = directory / "login-qr.png"
    await asyncio.to_thread(path.write_bytes, data)
    return path
