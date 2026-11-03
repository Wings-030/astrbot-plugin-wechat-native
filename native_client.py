"""agent-wechat-native v1.3.3 /v1 transport; no AstrBot OpenAPI dependency."""

import asyncio
import hashlib
import io
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

import aiohttp
from aiohttp.http_exceptions import LineTooLong

from .config import (
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_PDF_PAGES,
    MAX_PDF_TEXT_CHARS,
    MEDIA_DOWNLOAD_TIMEOUT,
)
from .state import cursor_number

logger = logging.getLogger("astrbot")

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
    "media_not_available",
    "media_asset_not_found",
    "media_preview_unavailable",
    "media_variant_unavailable",
    "media_metadata_unavailable",
    "media_metadata_invalid",
    "media_metadata_limit",
    "media_key_missing",
    "media_identity_unavailable",
    "media_source_changed",
    "media_encoder_unavailable",
    "media_expired",
    "media_integrity_failed",
    "message_recalled",
    "unsupported_media",
    "unsupported_application_media",
    "unsupported_attachment_encryption",
    "unsupported_attachment_transport",
    "image_quality_unavailable",
    "image_size_limit",
    "source_size_limit",
    "image_decode_failed",
    "image_decode_limit",
    "image_format_unsupported",
    "wxgf_invalid",
    "wxgf_decode_failed",
    "wxgf_decoder_unavailable",
    "wxgf_decode_timeout",
    "wxgf_preview_limit",
    "mention_member_unavailable",
    "mention_group_membership_unavailable",
    "group_contact_unavailable",
    "group_state_incomplete",
}

MEDIA_ERROR_MESSAGES = {
    "unavailable": "图片无法读取，请重新发送。",
    "expired": "引用的图片已失效，请重新发送。",
    "too_large": "图片太大，暂时无法处理。",
    "missing_scope": "图片读取功能未正确配置，请联系管理员。",
    "unsupported_format": "暂不支持这种图片格式。",
    "recalled": "",
    "recent_unavailable": "未找到你在本群最近2分钟发送的图片，请重新发送或引用图片。",
}

PDF_ERROR_MESSAGES = {
    "unavailable": "PDF 文件无法读取，请重新发送。",
    "expired": "引用的 PDF 文件已失效，请重新发送。",
    "too_large": "PDF 文件太大，暂时无法处理。",
    "missing_scope": "文件读取功能未正确配置，请联系管理员。",
    "unsupported_format": "PDF 文件已损坏或格式无效，请检查后重试。",
    "recalled": "",
    "recent_unavailable": "未找到你在本群最近2分钟发送的 PDF 文件，请重新发送或引用该文件。",
    "encrypted_pdf": "该 PDF 文件已加密，暂时无法解析内容。",
    "empty_pdf_text": "未能从该 PDF 中提取到文字（可能是扫描件或纯图片 PDF），暂时无法解析。",
    "not_pdf": "",
}

_VARIANT_RANK = {"original": 0, "standard": 1, "thumbnail": 2}
_NON_TRANSFERABLE_STATES = {
    "available",
    "unavailable",
    "expired",
    "unsupported",
    "recalled",
    "failed",
}


class NativeAPIError(Exception):
    def __init__(self, status: int, code: str = "request_failed"):
        self.status = status
        self.code = code if code in _SAFE_CODES else "request_failed"
        super().__init__(f"Native API HTTP {status}: {self.code}")


class MediaError(Exception):
    def __init__(self, reason: str = "unavailable", *, media_kind: str = "image"):
        self.media_kind = "pdf" if media_kind == "pdf" else "image"
        messages = PDF_ERROR_MESSAGES if self.media_kind == "pdf" else MEDIA_ERROR_MESSAGES
        self.reason = reason if reason in messages else "unavailable"
        self.user_message = messages[self.reason]
        super().__init__(f"Native media error: {self.reason}")


class QuotedMediaMetadataError(MediaError):
    """Native rejected the embedded quote's download metadata."""

    def __init__(self, *, media_kind: str = "image"):
        super().__init__("unavailable", media_kind=media_kind)


class SendError(Exception):
    """A retained send requires inspection. Never automatically create a new key."""

    def __init__(self, state: str, key: str, send_id: str | None = None):
        self.state, self.key, self.send_id = state, key, send_id
        super().__init__(f"Native send requires inspection: {state}")


class GroupMembersUnavailable(ValueError):
    """Mention metadata could not be prepared; no send has been submitted."""

    def __init__(self, state: str, code: str = "operation_failed", operation_id: str | None = None):
        self.state = state
        self.code = "operation_failed"
        if isinstance(code, str) and code in {
            "operation_failed",
            "auth_required",
            "account_identity_mismatch",
            "members_unavailable",
            "timeout",
        }:
            self.code = code
        self.operation_id = None
        if operation_id is not None:
            try:
                self.operation_id = str(UUID(operation_id))
            except ValueError:
                pass
        super().__init__(
            f"Native 群成员资料不可用：状态 {self.state}，错误码 {self.code}，"
            f"操作 {self.operation_id or 'unknown'}"
        )


def _check_guard(guard) -> None:
    if guard is not None:
        guard()


def _effective_variant(asset: dict) -> str:
    actual = asset.get("actualVariant")
    if isinstance(actual, str) and actual:
        return actual
    variant = asset.get("variant")
    return variant if isinstance(variant, str) else ""


def _extract_job_error_code(job: dict) -> str:
    for key in ("error", "result"):
        val = job.get(key)
        if isinstance(val, dict) and isinstance(val.get("code"), str):
            return val["code"]
        if isinstance(val, str) and val:
            return val
    for key in ("reason", "errorCode", "code"):
        val = job.get(key)
        if isinstance(val, str) and val:
            return val
    return "request_failed"


def _map_media_code(
    code: str | None, *, quoted: bool = False, status: int = 0, media_kind: str = "image"
) -> MediaError:
    if status in {401, 403} or code in {"unauthorized", "forbidden", "insufficient_scope"}:
        return MediaError("missing_scope", media_kind=media_kind)
    if code == "message_recalled" or (status == 410 and code != "event_cursor_expired"):
        return MediaError("recalled", media_kind=media_kind)
    if status == 413 or code in {"image_size_limit", "source_size_limit", "wxgf_preview_limit"}:
        return MediaError("too_large", media_kind=media_kind)
    if code == "media_expired" or (
        quoted
        and status == 404
        and code in {"not_found", "media_not_cached", "media_asset_not_found"}
    ):
        return MediaError("expired", media_kind=media_kind)
    if code in {
        "unsupported_media",
        "unsupported_application_media",
        "unsupported_attachment_encryption",
        "unsupported_attachment_transport",
        "image_format_unsupported",
        "wxgf_invalid",
        "wxgf_decode_failed",
        "wxgf_decoder_unavailable",
        "wxgf_decode_timeout",
    }:
        return MediaError("unsupported_format", media_kind=media_kind)
    return MediaError("unavailable", media_kind=media_kind)


def is_pdf_filename(filename: str | None) -> bool:
    if not isinstance(filename, str):
        return False
    cleaned = filename.strip()
    if (
        not cleaned
        or len(cleaned) > 255
        or "/" in cleaned
        or "\\" in cleaned
        or any(ord(c) < 32 or ord(c) == 127 for c in cleaned)
    ):
        return False
    return cleaned.lower().endswith(".pdf")


def validate_pdf_bytes(data: bytes, *, max_bytes: int = MAX_PDF_BYTES) -> str:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MediaError("unavailable", media_kind="pdf")
    if len(data) > max_bytes:
        raise MediaError("too_large", media_kind="pdf")
    if data[:4].lower() == b"wxgf" or data.lstrip()[:1] == b"<":
        raise MediaError("unsupported_format", media_kind="pdf")
    header_pos = data[:1024].find(b"%PDF-")
    if header_pos < 0 or len(data) < header_pos + 8:
        raise MediaError("unsupported_format", media_kind="pdf")
    return "application/pdf"


def extract_pdf_text(
    data: bytes,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_PDF_TEXT_CHARS,
) -> str:
    """Extract bounded text from an in-memory PDF. Nothing is written to disk."""
    validate_pdf_bytes(data, max_bytes=max_bytes)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(bytes(data)), strict=False)
        if reader.is_encrypted:
            try:
                decrypted = reader.decrypt("")
            except Exception:
                decrypted = 0
            if not decrypted:
                raise MediaError("encrypted_pdf", media_kind="pdf")
        total_pages = len(reader.pages)
    except MediaError:
        raise
    except Exception:
        raise MediaError("unsupported_format", media_kind="pdf") from None
    if total_pages <= 0:
        raise MediaError("empty_pdf_text", media_kind="pdf")
    sections: list[str] = []
    size = 0
    try:
        for index in range(min(total_pages, max_pages)):
            text = (reader.pages[index].extract_text() or "").replace("\x00", "").strip()
            if not text:
                continue
            section = f"[第 {index + 1} 页]\n{text}"
            sections.append(section)
            size += len(section) + 2
            if size > max_chars:
                break
    except Exception:
        if not sections:
            raise MediaError("unsupported_format", media_kind="pdf") from None
    if not sections:
        raise MediaError("empty_pdf_text", media_kind="pdf")
    combined = "\n\n".join(sections)
    truncated = total_pages > max_pages or len(combined) > max_chars
    if len(combined) > max_chars:
        combined = combined[:max_chars].rstrip()
    if truncated:
        combined += f"\n\n[注：该 PDF 共 {total_pages} 页，内容已截取前部供分析]"
    return combined


def _jpeg_has_end_marker(data: bytes | bytearray) -> bool:
    """Walk JPEG framing; metadata may contain EOI bytes and a photo may have a trailer."""
    position = 2
    in_scan = False
    saw_scan = False
    while position < len(data):
        if in_scan:
            position = data.find(b"\xff", position)
            if position < 0:
                return False
        if data[position] != 0xFF:
            return False
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position == len(data):
            return False
        marker = data[position]
        position += 1
        if in_scan and (marker == 0x00 or 0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xD9:
            return saw_scan
        if marker == 0x00 or 0xD0 <= marker <= 0xD8:
            return False
        if marker == 0x01:
            continue
        if position + 2 > len(data):
            return False
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            return False
        position += length
        if marker == 0xDA:
            saw_scan = True
            in_scan = True
        elif marker != 0xDC:
            in_scan = False
    return False


def validate_image_bytes(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MediaError("unavailable")
    if len(data) > max_bytes:
        raise MediaError("too_large")
    if data[:4].lower() == b"wxgf" or data.lstrip()[:1] == b"<":
        raise MediaError("unsupported_format")
    if data.startswith(b"\xff\xd8\xff"):
        if len(data) < 12 or not _jpeg_has_end_marker(data):
            raise MediaError("unavailable")
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if (
            len(data) < 33
            or data[12:16] != b"IHDR"
            or int.from_bytes(data[16:20], "big") <= 0
            or int.from_bytes(data[20:24], "big") <= 0
            or not data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        ):
            raise MediaError("unavailable")
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        if (
            len(data) < 14
            or int.from_bytes(data[6:8], "little") <= 0
            or int.from_bytes(data[8:10], "little") <= 0
            or not data.endswith(b";")
        ):
            raise MediaError("unavailable")
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if (
            len(data) < 16
            or data[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}
            or int.from_bytes(data[4:8], "little") + 8 != len(data)
        ):
            raise MediaError("unavailable")
        return "image/webp"
    if data.startswith((b"\x89PNG", b"GIF8")):
        raise MediaError("unavailable")
    raise MediaError("unsupported_format")


class NativeClient:
    def __init__(
        self,
        api_url: str,
        account_id: str,
        token: str,
        *,
        request_timeout: float = 15,
        send_timeout: float = 65,
        media_timeout: float = MEDIA_DOWNLOAD_TIMEOUT,
        poll_interval: float = 0.25,
        idle_timeout: float = 45,
    ):
        self.api_url = api_url
        self.account_id = account_id
        self.prefix = f"/accounts/{quote(account_id, safe='')}"
        self._token = token
        self.request_timeout = request_timeout
        self.send_timeout, self.poll_interval = send_timeout, poll_interval
        self.media_timeout = media_timeout
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

    async def _raise_for_status(self, response) -> None:
        if 200 <= response.status < 300:
            return
        # Never include a server body, URL or underlying exception in logs.
        default_code = (
            "event_cursor_expired"
            if response.status == 410
            else "image_size_limit"
            if response.status == 413
            else "request_failed"
        )
        code = default_code
        try:
            body = await self._read_json(response)
            error = body.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
        except (NativeAPIError, aiohttp.ClientError, TimeoutError, OSError):
            pass  # Preserve the HTTP status even when its error body is unusable.
        if code not in _SAFE_CODES:
            code = default_code
        raise NativeAPIError(response.status, code)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self.closed:
            raise NativeAPIError(0)
        try:
            async with self._session.request(
                method, self.api_url + path, allow_redirects=False, **kwargs
            ) as response:
                await self._raise_for_status(response)
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

    async def media_assets(self, message_id: str) -> dict:
        return await self._request(
            "GET", self.prefix + "/messages/" + quote(message_id, safe="") + "/media"
        )

    async def transfer_media(
        self, message_id: str, quality: str = "original", *, asset_id: str | None = None
    ) -> dict:
        logger.debug(
            "WeChat Native 请求媒体传输：message=%s quality=%s asset=%s",
            message_id,
            quality,
            asset_id,
        )
        payload = {"messageId": message_id, "quality": quality}
        if asset_id is not None:
            payload["assetId"] = asset_id
        try:
            return await self._request(
                "POST",
                self.prefix + "/messages/" + quote(message_id, safe="") + "/media-transfers",
                json=payload,
            )
        except NativeAPIError as error:
            logger.warning(
                "WeChat Native 媒体传输请求失败：message=%s http_status=%s code=%s",
                message_id,
                error.status,
                error.code,
            )
            raise

    async def job(self, job_id: str) -> dict:
        return await self._request("GET", self.prefix + "/jobs/" + quote(job_id, safe=""))

    async def media_content(
        self,
        asset_id: str,
        *,
        preview: bool = False,
        max_bytes: int = MAX_IMAGE_BYTES,
    ) -> tuple[bytes, str]:
        if self.closed:
            raise NativeAPIError(0)
        path = self.prefix + "/media/" + quote(asset_id, safe="") + "/content"
        params = {"representation": "preview"} if preview else None
        try:
            async with self._session.get(
                self.api_url + path, params=params, allow_redirects=False
            ) as response:
                await self._raise_for_status(response)
                raw_length = response.headers.get("Content-Length")
                expected_length = None
                if raw_length is not None:
                    if not raw_length.strip().isdigit():
                        response.close()
                        raise NativeAPIError(502)
                    expected_length = int(raw_length)
                    if expected_length > max_bytes:
                        response.close()
                        raise MediaError("too_large")
                data = bytearray()
                chunk_size = max(1, min(65536, max_bytes + 1))
                async for chunk in response.content.iter_chunked(chunk_size):
                    if len(data) + len(chunk) > max_bytes:
                        response.close()
                        raise MediaError("too_large")
                    data.extend(chunk)
                if expected_length is not None and len(data) != expected_length:
                    raise MediaError("unavailable")
                return bytes(data), response.content_type or "application/octet-stream"
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise NativeAPIError(0, "timeout") from None

    async def _load_image_assets(self, message_id: str, *, quoted: bool, guard=None) -> list[dict]:
        return await self._load_assets(message_id, "image", quoted=quoted, guard=guard)

    async def _load_assets(
        self, message_id: str, kind: str, *, quoted: bool, guard=None
    ) -> list[dict]:
        _check_guard(guard)
        logger.debug("WeChat Native 查询图片资产：message=%s quoted=%s", message_id, quoted)
        try:
            body = await self.media_assets(message_id)
        except NativeAPIError as error:
            if quoted and error.status == 409 and error.code == "media_metadata_invalid":
                raise QuotedMediaMetadataError() from None
            logger.warning(
                "WeChat Native 图片资产查询失败：message=%s http_status=%s code=%s",
                message_id,
                error.status,
                error.code,
            )
            raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
        _check_guard(guard)
        items = body.get("items")
        if not isinstance(items, list):
            logger.warning("WeChat Native 图片资产响应格式无效：message=%s", message_id)
            raise MediaError("unavailable")
        logger.debug("WeChat Native 图片资产查询完成：message=%s count=%s", message_id, len(items))
        allowed_sources = {None, "quote"} if quoted else {None, "message"}
        images = []
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("accountId") != self.account_id
                or item.get("messageId") != message_id
                or not isinstance(item.get("id"), str)
                or not item["id"]
            ):
                logger.warning("WeChat Native 图片资产身份不匹配：message=%s", message_id)
                raise MediaError("unavailable")
            if item.get("kind") != kind or item.get("source") not in allowed_sources:
                continue
            reason = item.get("reason")
            logger.debug(
                "WeChat Native 图片资产：message=%s asset=%s variant=%s actual_variant=%s "
                "state=%s content_type=%s bytes=%s code=%s",
                message_id,
                item["id"],
                item.get("variant"),
                item.get("actualVariant"),
                item.get("state"),
                item.get("contentType"),
                item.get("byteLength"),
                reason if isinstance(reason, str) and reason in _SAFE_CODES else None,
            )
            if item.get("state") == "recalled" or item.get("reason") == "message_recalled":
                raise MediaError("recalled")
            images.append(item)
        if not images:
            logger.warning(
                "WeChat Native 未找到匹配的图片资产：message=%s quoted=%s", message_id, quoted
            )
            raise MediaError("expired" if quoted else "unavailable")
        return images

    async def _poll_media_job(
        self,
        job_id: str | None,
        *,
        message_id: str,
        initial: dict | None = None,
        asset_id: str | None = None,
        quoted: bool = False,
        guard=None,
    ) -> dict:
        if not isinstance(job_id, str) or not job_id:
            raise MediaError("unavailable")
        current = initial
        last_state = None
        while True:
            _check_guard(guard)
            if current is None:
                try:
                    current = await self.job(job_id)
                except NativeAPIError as error:
                    retryable = error.status in {0, 408, 429} or 500 <= error.status < 600
                    logger.log(
                        logging.DEBUG if retryable else logging.WARNING,
                        "WeChat Native 媒体任务查询失败：message=%s job=%s http_status=%s code=%s",
                        message_id,
                        job_id,
                        error.status,
                        error.code,
                    )
                    if retryable:
                        await asyncio.sleep(self.poll_interval)
                        continue
                    raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
                _check_guard(guard)
            if (
                not isinstance(current, dict)
                or current.get("id") != job_id
                or current.get("accountId") != self.account_id
                or current.get("kind") not in {None, "download", "media"}
                or (current.get("messageId") is not None and current.get("messageId") != message_id)
                or (
                    asset_id is not None
                    and current.get("assetId") is not None
                    and current.get("assetId") != asset_id
                )
            ):
                logger.warning(
                    "WeChat Native 媒体任务身份不匹配：message=%s job=%s", message_id, job_id
                )
                raise MediaError("unavailable")
            state = current.get("state")
            if state != last_state:
                logger.debug(
                    "WeChat Native 媒体任务状态：message=%s job=%s state=%s",
                    message_id,
                    job_id,
                    state,
                )
                last_state = state
            if state in {"available", "completed", "succeeded"}:
                return current
            if isinstance(state, str) and state in {
                "failed",
                "partial",
                "cancelled",
                "interrupted",
                "expired",
                "recalled",
                "needs_review",
            }:
                if state == "recalled":
                    raise MediaError("recalled")
                if state == "expired":
                    raise MediaError("expired")
                code = _extract_job_error_code(current)
                logger.warning(
                    "WeChat Native 媒体任务失败：message=%s job=%s code=%s",
                    message_id,
                    job_id,
                    code if code in _SAFE_CODES else "request_failed",
                )
                raise _map_media_code(code, quoted=quoted)
            if not isinstance(state, str) or state not in {"queued", "running", "pending"}:
                raise MediaError("unavailable")
            current = None
            await asyncio.sleep(self.poll_interval)

    async def _download_validated_asset(
        self, asset: dict, *, max_bytes: int, quoted: bool, guard=None
    ) -> bytes:
        _check_guard(guard)
        is_wxgf = (
            asset.get("contentType") == "image/x-wxgf"
            or str(asset.get("format", "")).lower() == "wxgf"
        )
        logger.info(
            "WeChat Native 下载图片资产：message=%s asset=%s variant=%s preview=%s",
            asset.get("messageId"),
            asset["id"],
            _effective_variant(asset),
            is_wxgf,
        )
        if is_wxgf:
            preview = asset.get("preview")
            if preview is True or asset.get("previewAvailable") is True:
                preview_meta = {}
            elif isinstance(preview, dict):
                preview_meta = preview
            else:
                raise MediaError("unsupported_format")
            if preview_meta.get("state") in {"unavailable", "unsupported", "failed"}:
                raise MediaError("unsupported_format")
            preview_type = preview_meta.get("contentType")
            if preview_type not in {None, "image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise MediaError("unsupported_format")
            preview_len = preview_meta.get("byteLength")
            if isinstance(preview_len, int):
                if preview_len <= 0:
                    raise MediaError("unsupported_format")
                if preview_len > max_bytes:
                    raise MediaError("too_large")
            try:
                data, _ = await self.media_content(asset["id"], preview=True, max_bytes=max_bytes)
            except NativeAPIError as error:
                logger.warning(
                    "WeChat Native 图片预览下载失败：asset=%s http_status=%s code=%s",
                    asset["id"],
                    error.status,
                    error.code,
                )
                raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
            _check_guard(guard)
            if isinstance(preview_len, int) and len(data) != preview_len:
                logger.warning(
                    "WeChat Native 图片预览长度不匹配：asset=%s expected=%s actual=%s",
                    asset["id"],
                    preview_len,
                    len(data),
                )
                raise MediaError("unavailable")
            preview_sha = preview_meta.get("sha256")
            if (
                isinstance(preview_sha, str)
                and preview_sha
                and hashlib.sha256(data).hexdigest().lower() != preview_sha.lower()
            ):
                logger.warning("WeChat Native 图片预览散列校验失败：asset=%s", asset["id"])
                raise MediaError("unavailable")
        else:
            byte_len = asset.get("byteLength")
            if isinstance(byte_len, int):
                if byte_len <= 0:
                    raise MediaError("unavailable")
                if byte_len > max_bytes:
                    raise MediaError("too_large")
            adv_len = asset.get("advertisedByteLength")
            if isinstance(adv_len, int) and adv_len > max_bytes:
                raise MediaError("too_large")
            try:
                data, _ = await self.media_content(asset["id"], preview=False, max_bytes=max_bytes)
            except NativeAPIError as error:
                logger.warning(
                    "WeChat Native 图片内容下载失败：asset=%s http_status=%s code=%s",
                    asset["id"],
                    error.status,
                    error.code,
                )
                raise _map_media_code(error.code, quoted=quoted, status=error.status) from None
            _check_guard(guard)
            if isinstance(byte_len, int) and len(data) != byte_len:
                logger.warning(
                    "WeChat Native 图片内容长度不匹配：asset=%s expected=%s actual=%s",
                    asset["id"],
                    byte_len,
                    len(data),
                )
                raise MediaError("unavailable")
            sha = asset.get("sha256")
            if (
                isinstance(sha, str)
                and sha
                and hashlib.sha256(data).hexdigest().lower() != sha.lower()
            ):
                logger.warning("WeChat Native 图片内容散列校验失败：asset=%s", asset["id"])
                raise MediaError("unavailable")
        try:
            content_type = validate_image_bytes(data, max_bytes=max_bytes)
        except MediaError as error:
            logger.warning(
                "WeChat Native 图片格式校验失败：asset=%s bytes=%s reason=%s",
                asset["id"],
                len(data),
                error.reason,
            )
            raise
        logger.debug(
            "WeChat Native 图片校验通过：asset=%s content_type=%s bytes=%s",
            asset["id"],
            content_type,
            len(data),
        )
        return data

    async def fetch_image(
        self,
        message_id: str,
        *,
        quoted: bool = False,
        max_bytes: int = MAX_IMAGE_BYTES,
        guard=None,
    ) -> bytes:
        try:
            async with asyncio.timeout(self.media_timeout):
                assets = await self._load_image_assets(message_id, quoted=quoted, guard=guard)
                errors: list[MediaError] = []
                tried_ids: set[str] = set()

                high_available = sorted(
                    (
                        a
                        for a in assets
                        if a.get("state") == "available"
                        and _effective_variant(a) in {"original", "standard"}
                    ),
                    key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                )
                for asset in high_available:
                    tried_ids.add(asset["id"])
                    try:
                        return await self._download_validated_asset(
                            asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                        )
                    except MediaError as error:
                        if error.reason in {"recalled", "missing_scope"}:
                            raise
                        errors.append(error)

                if not high_available:
                    transfer_candidates = sorted(
                        (
                            a
                            for a in assets
                            if a.get("variant") in {"original", "standard"}
                            and a.get("state") not in _NON_TRANSFERABLE_STATES
                        ),
                        key=lambda a: _VARIANT_RANK.get(a.get("variant", ""), 99),
                    )
                    if not transfer_candidates and not any(
                        a.get("variant") in {"original", "standard"} for a in assets
                    ):
                        transfer_candidates = [
                            a
                            for a in assets
                            if a.get("variant") == "thumbnail"
                            and a.get("state") not in _NON_TRANSFERABLE_STATES
                        ]
                        if not transfer_candidates and not quoted:
                            transfer_candidates = [{"id": None, "variant": "original"}]
                    for candidate in transfer_candidates:
                        size = (
                            candidate.get("byteLength")
                            if isinstance(candidate.get("byteLength"), int)
                            else candidate.get("advertisedByteLength")
                        )
                        if isinstance(size, int) and size > max_bytes:
                            errors.append(MediaError("too_large"))
                            continue
                        active_job = candidate.get("job")
                        active_job_id = (
                            candidate.get("jobId")
                            or candidate.get("activeJobId")
                            or (active_job.get("id") if isinstance(active_job, dict) else None)
                        )
                        try:
                            if isinstance(active_job_id, str) and active_job_id:
                                logger.debug(
                                    "WeChat Native 复用媒体任务：message=%s job=%s",
                                    message_id,
                                    active_job_id,
                                )
                                await self._poll_media_job(
                                    active_job_id,
                                    message_id=message_id,
                                    initial=active_job if isinstance(active_job, dict) else None,
                                    asset_id=candidate.get("id"),
                                    quoted=quoted,
                                    guard=guard,
                                )
                            else:
                                _check_guard(guard)
                                try:
                                    job = await self.transfer_media(
                                        message_id, candidate.get("variant") or "original"
                                    )
                                except NativeAPIError as error:
                                    raise _map_media_code(
                                        error.code, quoted=quoted, status=error.status
                                    ) from None
                                _check_guard(guard)
                                await self._poll_media_job(
                                    job.get("id"),
                                    message_id=message_id,
                                    initial=job,
                                    asset_id=candidate.get("id"),
                                    quoted=quoted,
                                    guard=guard,
                                )
                            assets = await self._load_image_assets(
                                message_id, quoted=quoted, guard=guard
                            )
                            refreshed_high = sorted(
                                (
                                    a
                                    for a in assets
                                    if a.get("state") == "available"
                                    and _effective_variant(a) in {"original", "standard"}
                                    and a["id"] not in tried_ids
                                ),
                                key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                            )
                            for asset in refreshed_high:
                                tried_ids.add(asset["id"])
                                try:
                                    return await self._download_validated_asset(
                                        asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                                    )
                                except MediaError as error:
                                    if error.reason in {"recalled", "missing_scope"}:
                                        raise
                                    errors.append(error)
                        except MediaError as error:
                            if error.reason in {"recalled", "missing_scope"}:
                                raise
                            logger.warning(
                                "WeChat Native 高清传输失败，尝试回退：message=%s reason=%s",
                                message_id,
                                error.reason,
                            )
                            errors.append(error)
                        break

                fallbacks = sorted(
                    (
                        a
                        for a in assets
                        if a.get("state") == "available" and a["id"] not in tried_ids
                    ),
                    key=lambda a: _VARIANT_RANK.get(_effective_variant(a), 99),
                )
                for asset in fallbacks:
                    tried_ids.add(asset["id"])
                    try:
                        return await self._download_validated_asset(
                            asset, max_bytes=max_bytes, quoted=quoted, guard=guard
                        )
                    except MediaError as error:
                        if error.reason in {"recalled", "missing_scope"}:
                            raise
                        errors.append(error)

                reasons = {e.reason for e in errors}
                for asset in assets:
                    state = asset.get("state")
                    code = asset.get("reason") or asset.get("errorCode")
                    if state == "recalled" or code == "message_recalled":
                        reasons.add("recalled")
                    elif state == "expired" or code == "media_expired":
                        reasons.add("expired")
                    elif state == "unsupported" or (
                        isinstance(code, str)
                        and _map_media_code(code, quoted=quoted).reason == "unsupported_format"
                    ):
                        reasons.add("unsupported_format")
                    elif isinstance(code, str):
                        reasons.add(_map_media_code(code, quoted=quoted).reason)
                for priority in (
                    "recalled",
                    "missing_scope",
                    "too_large",
                    "expired",
                    "unsupported_format",
                ):
                    if priority in reasons:
                        raise MediaError(priority)
                raise MediaError("unavailable")
        except TimeoutError:
            logger.warning(
                "WeChat Native 图片读取超时：message=%s timeout_seconds=%s",
                message_id,
                self.media_timeout,
            )
            raise MediaError("unavailable") from None

    async def fetch_pdf(
        self, message_id: str, *, max_bytes: int = MAX_PDF_BYTES, guard=None
    ) -> tuple[bytes, str]:
        """Download a validated PDF attachment as ``(bytes, filename)``; memory only."""
        try:
            async with asyncio.timeout(self.media_timeout):
                return await self._fetch_pdf(message_id, max_bytes=max_bytes, guard=guard)
        except TimeoutError:
            logger.warning(
                "WeChat Native PDF 读取超时：message=%s timeout_seconds=%s",
                message_id,
                self.media_timeout,
            )
            raise MediaError("unavailable", media_kind="pdf") from None
        except MediaError as error:
            if error.media_kind == "pdf":
                raise
            raise MediaError(error.reason, media_kind="pdf") from None

    async def _load_pdf_assets(self, message_id: str, guard=None) -> list[dict]:
        assets = await self._load_assets(message_id, "file", quoted=False, guard=guard)
        pdfs = [
            asset
            for asset in assets
            if _effective_variant(asset) in {"original", ""}
            and is_pdf_filename(asset.get("filename"))
        ]
        if not pdfs:
            # Native exposes the attachment's name only through its media asset.
            raise MediaError("not_pdf", media_kind="pdf")
        return pdfs

    async def _fetch_pdf(self, message_id: str, *, max_bytes: int, guard=None) -> tuple[bytes, str]:
        asset = (await self._load_pdf_assets(message_id, guard))[0]
        state = asset.get("state")
        size = asset.get("byteLength")
        if not isinstance(size, int):
            size = asset.get("advertisedByteLength")
        if isinstance(size, int) and size > max_bytes:
            raise MediaError("too_large", media_kind="pdf")
        if state != "available":
            if state in {"expired", "recalled", "unsupported", "unavailable", "failed"}:
                reason = {
                    "expired": "expired",
                    "recalled": "recalled",
                    "unsupported": "unsupported_format",
                }.get(state)
                raise MediaError(reason or "unavailable", media_kind="pdf")
            job = asset.get("job")
            job_id = (
                asset.get("jobId")
                or asset.get("activeJobId")
                or (job.get("id") if isinstance(job, dict) else None)
            )
            initial = job if isinstance(job, dict) else None
            if not isinstance(job_id, str) or not job_id:
                _check_guard(guard)
                try:
                    initial = await self.transfer_media(message_id, "original")
                except NativeAPIError as error:
                    raise _map_media_code(
                        error.code, status=error.status, media_kind="pdf"
                    ) from None
                job_id = initial.get("id")
            _check_guard(guard)
            await self._poll_media_job(
                job_id, message_id=message_id, initial=initial, asset_id=asset["id"], guard=guard
            )
            refreshed = await self._load_pdf_assets(message_id, guard)
            asset = next((a for a in refreshed if a.get("state") == "available"), None)
            if asset is None:
                raise MediaError("unavailable", media_kind="pdf")
        byte_len = asset.get("byteLength")
        if isinstance(byte_len, int) and byte_len <= 0:
            raise MediaError("unavailable", media_kind="pdf")
        adv_len = asset.get("advertisedByteLength")
        if isinstance(byte_len, int) and byte_len > max_bytes:
            raise MediaError("too_large", media_kind="pdf")
        if isinstance(adv_len, int) and adv_len > max_bytes:
            raise MediaError("too_large", media_kind="pdf")
        _check_guard(guard)
        try:
            data, _ = await self.media_content(asset["id"], preview=False, max_bytes=max_bytes)
        except NativeAPIError as error:
            logger.warning(
                "WeChat Native PDF 内容下载失败：asset=%s http_status=%s code=%s",
                asset["id"],
                error.status,
                error.code,
            )
            raise _map_media_code(error.code, status=error.status, media_kind="pdf") from None
        except MediaError as error:
            raise MediaError(error.reason, media_kind="pdf") from None
        _check_guard(guard)
        if isinstance(byte_len, int) and len(data) != byte_len:
            raise MediaError("unavailable", media_kind="pdf")
        sha = asset.get("sha256")
        if isinstance(sha, str) and sha and hashlib.sha256(data).hexdigest().lower() != sha.lower():
            logger.warning("WeChat Native PDF 内容散列校验失败：asset=%s", asset["id"])
            raise MediaError("unavailable", media_kind="pdf")
        validate_pdf_bytes(data, max_bytes=max_bytes)
        logger.debug("WeChat Native PDF 下载完成：asset=%s bytes=%s", asset["id"], len(data))
        return data, asset["filename"].strip()

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
        operation_id = None
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
                    state = operation.get("state")
                    if state == "succeeded":
                        break
                    if isinstance(state, str) and state in {"failed", "cancelled", "interrupted"}:
                        result = operation.get("result")
                        code = (
                            result.get("code") if isinstance(result, dict) else "operation_failed"
                        )
                        raise GroupMembersUnavailable(state, code, operation_id)
                    if not isinstance(state, str) or state not in {"queued", "running"}:
                        raise ValueError("Native 群成员刷新状态无效，未发送")
                    await asyncio.sleep(self.poll_interval)
                    operation = await self._request(
                        "GET", self.prefix + "/operations/" + quote(operation_id, safe="")
                    )
                if not await self._group_members_present(thread, user_ids):
                    raise GroupMembersUnavailable("succeeded", "members_unavailable", operation_id)
        except TimeoutError:
            raise GroupMembersUnavailable("timeout", "timeout", operation_id) from None

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
