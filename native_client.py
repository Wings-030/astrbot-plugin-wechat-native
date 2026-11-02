"""agent-wechat-native v1.3.3 /v1 transport; no AstrBot OpenAPI dependency."""

import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import quote

import aiohttp
from aiohttp.http_exceptions import LineTooLong

from .state import cursor_number

_SAFE_CODES = {
    "event_cursor_expired",
    "invalid_cursor",
    "unauthorized",
    "forbidden",
    "not_found",
    "timeout",
    "account_not_found",
    "insufficient_scope",
    "media_not_cached",
    "mention_member_unavailable",
    "mention_group_membership_unavailable",
    "group_contact_unavailable",
    "group_state_incomplete",
}


class NativeAPIError(Exception):
    def __init__(self, status: int, code: str = "request_failed"):
        self.status = status
        self.code = code if code in _SAFE_CODES else "request_failed"
        super().__init__(f"Native API HTTP {status}: {self.code}")


class SendError(Exception):
    """A retained send requires inspection. Never automatically create a new key."""

    def __init__(self, state: str, key: str, send_id: str | None = None):
        self.state, self.key, self.send_id = state, key, send_id
        super().__init__(f"Native send requires inspection: {state}")


class NativeClient:
    def __init__(
        self,
        api_url: str,
        account_id: str,
        token: str,
        *,
        request_timeout: float = 15,
        send_timeout: float = 65,
        poll_interval: float = 0.25,
        idle_timeout: float = 45,
    ):
        self.api_url = api_url
        self.account_id = account_id
        self.prefix = f"/accounts/{quote(account_id, safe='')}"
        self._token = token
        self.request_timeout = request_timeout
        self.send_timeout, self.poll_interval = send_timeout, poll_interval
        self.idle_timeout = idle_timeout
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def open(self):
        if self.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                trust_env=False,  # Native commonly lives on a private Docker network.
                cookie_jar=aiohttp.DummyCookieJar(),
            )

    @property
    def closed(self):
        return self._session is None or self._session.closed

    async def close(self):
        if self._session:
            await self._session.close()

    async def _read_json(self, response) -> dict:
        data = bytearray()
        async for part in response.content.iter_chunked(16384):
            data.extend(part)
            if len(data) > 1024 * 1024:
                raise NativeAPIError(502)
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError):
            raise NativeAPIError(502) from None

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self.closed:
            raise NativeAPIError(0)
        try:
            async with self._session.request(
                method, self.api_url + path, allow_redirects=False, **kwargs
            ) as response:
                if not 200 <= response.status < 300:
                    # Never include a server body, URL or underlying exception in logs.
                    code = "event_cursor_expired" if response.status == 410 else "request_failed"
                    if response.status != 410:
                        try:
                            body = await self._read_json(response)
                            error = body.get("error")
                            if isinstance(error, dict) and isinstance(error.get("code"), str):
                                code = error["code"]
                        except (NativeAPIError, aiohttp.ClientError, TimeoutError, OSError):
                            pass  # Preserve the HTTP status even when its error body is unusable.
                    raise NativeAPIError(response.status, code)
                return await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None

    async def accounts(self):
        return await self._request("GET", "/accounts")

    async def status(self):
        return await self._request("GET", "/status")

    async def account(self):
        return await self._request("GET", self.prefix)

    async def message(self, message_id: str):
        return await self._request("GET", self.prefix + "/messages/" + quote(message_id, safe=""))

    async def send(self, send_id: str):
        return await self._request("GET", self.prefix + "/sends/" + quote(send_id, safe=""))

    async def send_by_key(self, key: str):
        return await self._request(
            "GET", self.prefix + "/sends/by-idempotency-key", params={"key": key}
        )

    async def _group_members_present(self, thread: str, user_ids: list[str]) -> bool:
        for username in user_ids:
            try:
                value = await self._request(
                    "GET",
                    self.prefix
                    + "/groups/"
                    + quote(thread, safe="")
                    + "/members/"
                    + quote(username, safe=""),
                )
            except NativeAPIError as error:
                if error.status == 404 and error.code in {"not_found", "media_not_cached"}:
                    return False
                raise
            if value.get("groupId") != thread or value.get("username") != username:
                raise ValueError("Native 群成员资料身份不匹配，未发送")
            if value.get("state") == "unconfirmed":
                return False
            member = value.get("member")
            if (
                value.get("state") != "present"
                or not isinstance(member, dict)
                or member.get("username") != username
            ):
                raise ValueError("Native 群成员资料无效，未发送")
        return True

    async def prepare_group_mentions(self, thread: str, user_ids: list[str]) -> None:
        """Refresh missing mention metadata before creating any send intent."""
        user_ids = list(dict.fromkeys(user_ids))
        try:
            async with asyncio.timeout(self.send_timeout):
                if await self._group_members_present(thread, user_ids):
                    return
                operation = await self._request(
                    "POST",
                    self.prefix + "/groups/" + quote(thread, safe="") + "/members/refresh",
                    json={},
                )
                operation_id = operation.get("id")
                while True:
                    if (
                        not isinstance(operation_id, str)
                        or not operation_id
                        or operation.get("id") != operation_id
                        or operation.get("accountId") != self.account_id
                        or operation.get("kind") != "metadata"
                    ):
                        raise ValueError("Native 群成员刷新回执身份不匹配，未发送")
                    if operation.get("state") == "succeeded":
                        break
                    if operation.get("state") not in {"queued", "running"}:
                        raise ValueError("Native 群成员资料刷新失败，未发送")
                    await asyncio.sleep(self.poll_interval)
                    operation = await self._request(
                        "GET", self.prefix + "/operations/" + quote(operation_id, safe="")
                    )
                if not await self._group_members_present(thread, user_ids):
                    raise ValueError("Native 群成员资料刷新后仍不可用，未发送")
        except TimeoutError:
            raise ValueError("Native 群成员资料刷新超时，未发送") from None

    async def send_text(
        self,
        thread: str,
        text: str,
        key: str,
        mentions: dict | None = None,
        *,
        quote: dict | None = None,
    ):
        if not text.strip() or len(text.encode("utf-8")) > 65536:
            raise ValueError("文本必须非空且不超过 64 KiB（含 @名称）")
        if quote is not None:
            if len(text.encode("utf-8")) > 16384:
                raise ValueError("Native 引用回复文本不能超过 16 KiB")
            if (
                not isinstance(quote, dict)
                or set(quote) != {"messageId", "serverId"}
                or any(not isinstance(value, str) or not value for value in quote.values())
            ):
                raise ValueError("Native 引用需要字符串 messageId 和 serverId")
        if not key or any(ord(x) < 33 or ord(x) > 126 for x in key):
            raise ValueError("无效 Idempotency-Key")
        if self.closed:
            raise ValueError("Native 客户端未连接，未提交发送")
        payload = {"threadId": thread, "text": text}
        if mentions:
            payload["mentions"] = mentions
        if quote is not None:
            payload["quote"] = quote
        try:
            value = await self._request(
                "POST", self.prefix + "/sends", json=payload, headers={"Idempotency-Key": key}
            )
        except NativeAPIError as error:
            if 400 <= error.status < 500 and error.status not in {408, 429}:
                raise
            try:
                value = await self.send_by_key(key)
            except NativeAPIError:
                raise SendError("submission_unknown", key) from None
        deadline = asyncio.get_running_loop().time() + self.send_timeout
        send_id = None
        poll_failed = False
        while True:
            if (
                not isinstance(value.get("id"), str)
                or not value["id"]
                or value.get("accountId") != self.account_id
                or value.get("destination") != thread
                or (send_id and send_id != value["id"])
            ):
                raise SendError("receipt_identity_mismatch", key, send_id)
            send_id = value["id"]
            state = value.get("state")
            if state == "accepted":
                return value  # Server acceptance, not proof of recipient delivery.
            if state not in {"queued", "submitting"}:
                safe_state = (
                    state if state in {"needs_review", "failed", "cancelled"} else "unknown"
                )
                raise SendError(safe_state, key, send_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SendError("status_unavailable" if poll_failed else state, key, send_id)
            await asyncio.sleep(min(self.poll_interval, remaining))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SendError("status_unavailable" if poll_failed else state, key, send_id)
            try:
                async with asyncio.timeout(remaining):
                    value = await self.send(send_id)
                poll_failed = False
            except NativeAPIError as error:
                if error.status in {0, 408, 429} or 500 <= error.status < 600:
                    poll_failed = True
                    continue  # Retry only the read, with the same send ID and deadline.
                raise SendError("status_unavailable", key, send_id) from None
            except TimeoutError:
                raise SendError(
                    "status_unavailable" if poll_failed else state, key, send_id
                ) from None

    async def events(self, after: str) -> AsyncIterator[dict]:
        cursor_number(after)
        if self.closed:
            raise NativeAPIError(0)
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=self.request_timeout, sock_read=self.idle_timeout
        )
        try:
            async with self._session.get(
                self.api_url + self.prefix + "/events/stream",
                params={"after": after},
                headers={"Accept": "text/event-stream", "Last-Event-ID": after},
                allow_redirects=False,
                timeout=timeout,
                read_bufsize=512 * 1024,  # StreamReader's line high-water limit is twice this.
            ) as response:
                if response.status != 200:
                    raise NativeAPIError(
                        response.status, "event_cursor_expired" if response.status == 410 else ""
                    )
                if response.content_type != "text/event-stream":
                    raise NativeAPIError(502)
                lines, event_type, event_id, size = [], "", "", 0
                async for raw in response.content:
                    try:
                        line = raw.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError:
                        raise NativeAPIError(502) from None
                    if line.startswith(":"):
                        continue  # Comments are not part of the pending event frame.
                    size += len(raw)
                    if size > 1024 * 1024:
                        raise NativeAPIError(502)
                    if not line:
                        if lines:
                            try:
                                value = json.loads("\n".join(lines))
                                if not isinstance(value, dict):
                                    raise ValueError
                                if event_type == "error":
                                    code = value.get("error", {}).get("code")
                                    raise NativeAPIError(
                                        410 if code == "event_cursor_expired" else 502, code
                                    )
                                cursor = value.get("cursor", event_id)
                                cursor_number(cursor)
                                if event_id and cursor != event_id:
                                    raise ValueError
                                if not isinstance(value.get("kind"), str) or not isinstance(
                                    value.get("data"), dict
                                ):
                                    raise ValueError
                                value["cursor"] = cursor
                            except (ValueError, AttributeError, TypeError):
                                raise NativeAPIError(502) from None
                            yield value
                        lines, event_type, event_id, size = [], "", "", 0
                    else:
                        field, _, data = line.partition(":")
                        if data.startswith(" "):
                            data = data[1:]
                        if field == "data":
                            lines.append(data)
                        elif field == "event":
                            event_type = data
                        elif field == "id":
                            event_id = data
                # An incomplete frame must be replayed from the last committed cursor.
        except (ValueError, LineTooLong):
            raise NativeAPIError(502) from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None
