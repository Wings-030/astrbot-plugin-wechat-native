"""Fetch a WeChat login QR through the Native HTTP API.

The daemon exposes the login operation and its QR image directly, so this stays
portable: no Docker access, CLI invocation, or host-specific paths are needed.
"""

import asyncio
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .native_client import MediaError, NativeAPIError, validate_image_bytes

QR_WAIT_SECONDS = 20
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "canceled", "expired", "logged_in"}


class LoginQRUnavailable(RuntimeError):
    """The daemon could not produce a scannable QR right now."""


@dataclass
class LoginQRSession:
    operation_id: str = ""
    request_key: str = field(default_factory=lambda: uuid4().hex)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def reset(self) -> None:
        self.operation_id = ""
        self.request_key = uuid4().hex


@contextmanager
def login_qr_file(data: bytes, directory: Path):
    """Keep a private, unique file alive only while the platform sends it."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="login-qr-", suffix=".png", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            path.chmod(0o600)
            stream.write(data)
        yield path
    finally:
        path.unlink(missing_ok=True)


async def create_login_qr(client, *, session: LoginQRSession | None = None) -> bytes:
    session = session if session is not None else LoginQRSession()
    async with session.lock:
        operation = None
        if session.operation_id:
            try:
                operation = await client.operation(session.operation_id)
            except NativeAPIError as error:
                if error.status != 404:
                    raise
                session.reset()
            if operation is not None and operation.get("state") in TERMINAL_STATES:
                session.reset()
                operation = None
        if operation is None:
            operation = await client.start_login(idempotency_key=session.request_key)
            operation_id = operation.get("id")
            if not isinstance(operation_id, str) or not operation_id:
                raise LoginQRUnavailable("login_operation_missing")
            session.operation_id = operation_id
        deadline = asyncio.get_running_loop().time() + QR_WAIT_SECONDS
        while True:
            state = str(operation.get("state") or "")
            if state in TERMINAL_STATES:
                session.reset()
                raise LoginQRUnavailable(state)
            result = operation.get("result") or {}
            generation = int(result.get("qrGeneration") or 0)
            if generation > 0 and result.get("qrAvailable"):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise LoginQRUnavailable("qr_timeout")
            await asyncio.sleep(1)
            operation = await client.operation(session.operation_id)
        data = await client.operation_qr(session.operation_id, generation)
        try:
            content_type = validate_image_bytes(data, max_bytes=2 * 1024 * 1024)
        except MediaError:
            raise LoginQRUnavailable("invalid_qr_image") from None
        if content_type != "image/png":
            raise LoginQRUnavailable("invalid_qr_image")
        return data
