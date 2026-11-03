"""Registered in-process AstrBot platform adapter."""

import asyncio
import hashlib
import random
import re
import time
import uuid
import weakref
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import Platform, PlatformMetadata, register_platform_adapter
from astrbot.api.star import StarTools

from .config import (
    ADAPTER_ID,
    CONFIG_METADATA,
    DEFAULT_CONFIG,
    MAX_CONCURRENT_MEDIA_TASKS,
    PLUGIN_ID,
    NativeConfig,
)
from .event import (
    WeChatNativeEvent,
    convert_message,
    message_timestamp,
    serialize_text,
    session_target,
)
from .native_client import (
    GroupMembersUnavailable,
    MediaError,
    NativeAPIError,
    NativeClient,
    QuotedMediaMetadataError,
    SendError,
)
from .state import StateStore, cursor_number
from .summary import GroupSummaryCache, is_summary_command


@register_platform_adapter(
    ADAPTER_ID,
    "直接连接 agent-wechat-native 的个人微信平台适配器",
    default_config_tmpl=DEFAULT_CONFIG.copy(),
    adapter_display_name="WeChat Native",
    logo_path="logo.svg",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
)
class WeChatNativeAdapter(Platform):
    reconnect_delay = 1.0
    time_warning_interval = 60.0

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
        *,
        state_dir: Path | None = None,
    ):
        super().__init__(platform_config, event_queue)
        self.native_config = NativeConfig.from_dict(platform_config)
        cfg = self.native_config
        self.client = NativeClient(cfg.api_url, cfg.account_id, cfg.load_token())
        # PlatformManager owns client_self_id (its instance/task UUID).
        self.bot_self_id = cfg.account_id
        root = state_dir if state_dir is not None else StarTools.get_data_dir(PLUGIN_ID)
        identity = f"{cfg.api_url}\n{cfg.account_id}\n{platform_config.get('id', ADAPTER_ID)}"
        self.state = StateStore(
            Path(root) / (hashlib.sha256(identity.encode()).hexdigest() + ".sqlite3")
        )
        self.stopped = False
        self._state_closed = False
        self._run_task = None
        self._send_tasks = set()
        self._media_tasks = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_TASKS)
        self._events = weakref.WeakValueDictionary()
        self._send_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._last_time_warning = float("-inf")
        self.summary_cache = GroupSummaryCache(cfg.summary_max_messages, cfg.summary_window_minutes)

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=ADAPTER_ID,
            description="WeChat Native",
            id=self.config.get("id", ADAPTER_ID),
            adapter_display_name="WeChat Native",
            support_streaming_message=False,
        )

    def get_client(self):
        return self.client

    def _check_timestamp(self, raw: dict) -> bool:
        stamp = message_timestamp(raw)
        now = time.time()
        if stamp is None or not -60 <= now - stamp <= 300:
            monotonic = time.monotonic()
            if monotonic - self._last_time_warning >= self.time_warning_interval:
                self._last_time_warning = monotonic
                delta = f"{now - stamp:.0f}" if stamp is not None else "未知"
                logger.warning(
                    "WeChat Native 实时消息因时间异常被忽略：时间差 %s 秒；"
                    "允许范围 -60 至 300 秒，请检查时钟或消息积压",
                    delta,
                )
            return False
        return True

    def _classify(self, raw: dict, origin: str) -> str:
        def ignored(reason: str) -> str:
            logger.debug("WeChat Native 消息忽略：message=%s reason=%s", raw.get("id"), reason)
            return "ignore"

        if origin != "live":
            return ignored("origin_not_live")
        if raw.get("direction") != "incoming":
            return ignored("not_incoming")
        if raw.get("recall") is not None:
            return ignored("recalled")
        if raw.get("senderId") == self.bot_self_id:
            return ignored("self_message")
        if not isinstance(raw.get("senderId"), str) or not raw["senderId"]:
            return ignored("missing_sender")
        cfg = self.native_config
        msg_type = raw.get("type")
        is_direct_image = msg_type == 3
        is_quote = msg_type == 49 and raw.get("appMessageType") == 57
        quote = raw.get("quote")
        is_quoted_image = is_quote and isinstance(quote, dict) and quote.get("type") == 3
        body = raw.get("body")
        has_text_body = isinstance(body, str) and bool(body.strip())
        if is_direct_image:
            if not cfg.enable_media:
                return ignored("media_disabled")
            if body is not None and not isinstance(body, str):
                return ignored("invalid_image_body")
        elif is_quote or msg_type == 1:
            if not has_text_body:
                return ignored("empty_text_body")
        else:
            return ignored("unsupported_message_type")
        if not self._check_timestamp(raw):
            return ignored("invalid_timestamp")
        thread = raw["threadId"]
        if not thread.endswith("@chatroom"):
            if not cfg.enable_private_reply:
                return ignored("private_reply_disabled")
            if cfg.allowed_users and raw["senderId"] not in cfg.allowed_users:
                return ignored("user_not_allowed")
            if cfg.enable_media and (is_direct_image or is_quoted_image):
                return "media"
            return "text" if has_text_body else ignored("empty_text_body")
        if cfg.group_reply_mode == "disabled":
            return ignored("group_reply_disabled")
        if cfg.allowed_groups and thread not in cfg.allowed_groups:
            return ignored("group_not_allowed")
        if is_direct_image:
            return ignored("direct_group_image")
        mentions = raw.get("mentions")
        mentioned = (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        )
        if mentioned and isinstance(body, str):
            stripped = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", body)
            if is_summary_command(stripped):
                return "text"
        if cfg.enable_media and is_quoted_image:
            return "media" if mentioned else ignored("quoted_image_without_mention")
        if cfg.group_reply_mode == "all" or mentioned:
            return "text"
        return ignored("group_without_mention")

    def _eligible(self, raw: dict, origin: str, *, summary: bool = False) -> bool:
        if not summary:
            return self._classify(raw, origin) != "ignore"
        cfg = self.native_config
        if (
            origin != "live"
            or raw.get("direction") != "incoming"
            or not (
                raw.get("type") == 1 or (raw.get("type") == 49 and raw.get("appMessageType") == 57)
            )
            or raw.get("recall") is not None
            or raw.get("senderId") == self.bot_self_id
            or not isinstance(raw.get("senderId"), str)
            or not raw["senderId"]
            or not isinstance(raw.get("body"), str)
            or not raw["body"].strip()
        ):
            return False
        if not self._check_timestamp(raw):
            return False
        thread = raw["threadId"]
        if not thread.endswith("@chatroom"):
            return False
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            return False
        return cfg.enable_group_summary

    async def _check_quote_original(
        self, raw: dict, wrapped: WeChatNativeEvent, original_id: str
    ) -> None:
        quote = raw["quote"]
        original = await self.client.message(original_id)
        if (
            original.get("id") != original_id
            or original.get("accountId") != self.native_config.account_id
            or original.get("threadId") != raw["threadId"]
            or original.get("serverId") != quote["serverId"]
            or original.get("senderId") != quote["senderId"]
            or original.get("type") != 3
        ):
            raise MediaError("unavailable")
        if (
            original.get("recall") is not None
            or self.state.message_status(original_id) == "recalled"
        ):
            wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise MediaError("recalled")

    async def _fetch_quote_original(self, raw: dict, wrapped: WeChatNativeEvent, guard) -> bytes:
        quote = raw["quote"]
        if not all(
            isinstance(quote.get(key), str) and quote[key] for key in ("serverId", "senderId")
        ):
            raise MediaError("unavailable")
        original_id = self.state.find_message_id(raw["threadId"], quote["serverId"])
        if original_id is None:
            raise MediaError("unavailable")
        try:
            guard()
            await self._check_quote_original(raw, wrapped, original_id)
            guard()
            logger.warning(
                "WeChat Native 引用媒体元数据无效，回退同聊天原图：message=%s original=%s",
                raw["id"],
                original_id,
            )
            image_bytes = await self.client.fetch_image(original_id, guard=guard)
            guard()
            await self._check_quote_original(raw, wrapped, original_id)
            current = await self.client.message(raw["id"])
            guard()
        except MediaError as error:
            if error.reason == "recalled":
                wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise
        except NativeAPIError as error:
            if error.status == 410 or error.code == "message_recalled":
                wrapped.set_extra("wechat_native_quote_invalidated", True)
                raise MediaError("recalled") from None
            raise MediaError(
                "missing_scope" if error.status in {401, 403} else "unavailable"
            ) from None
        if any(
            current.get(key) != raw.get(key) for key in ("id", "accountId", "threadId", "serverId")
        ):
            raise MediaError("unavailable")
        if current.get("recall") is not None:
            raise MediaError("recalled")
        current_quote = current.get("quote")
        if (
            current.get("type") != 49
            or current.get("appMessageType") != 57
            or not isinstance(current_quote, dict)
            or any(
                current_quote.get(key) != quote.get(key) for key in ("serverId", "senderId", "type")
            )
            or current_quote.get("state") != "embedded"
            or current_quote.get("embeddedImage") is not True
        ):
            wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise MediaError("recalled")
        wrapped.set_extra("wechat_native_quote_original_id", original_id)
        return image_bytes

    async def _process_media_message(self, raw: dict, wrapped: WeChatNativeEvent) -> None:
        task = asyncio.current_task()
        message_id = raw["id"]
        thread = raw["threadId"]
        is_quote = raw.get("type") == 49 and raw.get("appMessageType") == 57

        def guard() -> None:
            if (
                self.stopped
                or self._state_closed
                or wrapped.is_stopped()
                or wrapped.get_extra("wechat_native_quote_invalidated")
                or self.state.message_status(message_id) != "queued"
            ):
                raise MediaError("recalled")

        try:
            async with self._media_semaphore:
                guard()
                logger.info(
                    "WeChat Native 图片处理开始：message=%s quoted=%s", message_id, is_quote
                )
                if is_quote:
                    quote = raw.get("quote")
                    if not isinstance(quote, dict) or quote.get("type") != 3:
                        raise MediaError("unavailable")
                    state = quote.get("state")
                    logger.debug(
                        "WeChat Native 引用图片检查：message=%s state=%s embedded_image=%s",
                        message_id,
                        state,
                        quote.get("embeddedImage") is True,
                    )
                    if state == "recalled":
                        wrapped.set_extra("wechat_native_quote_invalidated", True)
                        wrapped.stop_event()
                        if (
                            not self._state_closed
                            and self.state.message_status(message_id) == "queued"
                        ):
                            self.state.mark_message(message_id, "no_reply")
                        return
                    if state == "unsupported":
                        raise MediaError("unsupported_format")
                    if state != "embedded" or quote.get("embeddedImage") is not True:
                        raise MediaError("expired")
                try:
                    async with asyncio.timeout(self.client.media_timeout):
                        try:
                            image_bytes = await self.client.fetch_image(
                                message_id, quoted=is_quote, guard=guard
                            )
                        except QuotedMediaMetadataError:
                            image_bytes = await self._fetch_quote_original(raw, wrapped, guard)
                except TimeoutError:
                    raise MediaError("unavailable") from None
                guard()
                logger.debug(
                    "WeChat Native 图片下载完成：message=%s bytes=%s", message_id, len(image_bytes)
                )
                enriched = convert_message(
                    raw, self.native_config, self.bot_self_id, image_bytes=image_bytes
                )
                event = WeChatNativeEvent(enriched, self)
                original_id = wrapped.get_extra("wechat_native_quote_original_id")
                if original_id:
                    event.set_extra("wechat_native_quote_original_id", original_id)
                try:
                    self.commit_event(event)
                except Exception:
                    self.state.mark_message(message_id, "needs_review")
                    logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                    raise RuntimeError("Native event handoff requires inspection") from None
                self._events[message_id] = event
                logger.info(
                    "WeChat Native 图片已交付 AstrBot：message=%s bytes=%s components=%s",
                    message_id,
                    len(image_bytes),
                    len(enriched.message),
                )
        except MediaError as error:
            log = logger.debug if error.reason == "recalled" else logger.warning
            log(
                "WeChat Native 图片处理终止：message=%s reason=%s",
                message_id,
                error.reason,
            )
            if error.reason == "recalled":
                if not self._state_closed and self.state.message_status(message_id) == "queued":
                    if wrapped.get_extra("wechat_native_quote_invalidated"):
                        self.state.mark_message(message_id, "no_reply")
                    else:
                        self.state.recall(thread, raw.get("serverId"), message_id)
                wrapped.stop_event()
                return
            if (
                self.stopped
                or self._state_closed
                or wrapped.is_stopped()
                or wrapped.get_extra("wechat_native_quote_invalidated")
                or self.state.message_status(message_id) != "queued"
            ):
                logger.debug(
                    "WeChat Native 图片错误提示已抑制：message=%s reason=event_invalidated",
                    message_id,
                )
                if (
                    not self._state_closed
                    and wrapped.get_extra("wechat_native_quote_invalidated")
                    and self.state.message_status(message_id) == "queued"
                ):
                    self.state.mark_message(message_id, "no_reply")
                return
            try:
                sent = await self.send_chain(
                    thread,
                    MessageChain([Plain(error.user_message)]),
                    source=raw,
                    event=wrapped,
                )
                logger.debug(
                    "WeChat Native 图片错误提示发送结果：message=%s sent=%s", message_id, sent
                )
                if (
                    not sent
                    and not self._state_closed
                    and self.state.message_status(message_id) == "queued"
                ):
                    self.state.mark_message(message_id, "no_reply")
            except (Exception, asyncio.CancelledError) as send_err:
                logger.error(
                    "WeChat Native 图片错误提示发送失败：message=%s error_type=%s",
                    message_id,
                    type(send_err).__name__,
                )
                if not self._state_closed:
                    current = self.state.message_status(message_id)
                    if current not in {"failed", "needs_review", "partial", "recalled"}:
                        self.state.mark_message(
                            message_id,
                            "failed" if isinstance(send_err, ValueError) else "needs_review",
                        )
                if isinstance(send_err, asyncio.CancelledError):
                    raise
        except asyncio.CancelledError:
            logger.debug(
                "WeChat Native 图片任务取消：message=%s stopped=%s", message_id, self.stopped
            )
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            if self.stopped:
                raise
        except Exception as error:
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            logger.error(
                "WeChat Native 图片处理失败：message=%s error_type=%s；消息标识已保留，需人工检查",
                message_id,
                type(error).__name__,
            )
        finally:
            self._media_tasks.discard(task)

    async def process_native_event(self, event: dict):
        if self.stopped:
            return
        cursor = event["cursor"]
        if cursor_number(cursor) <= cursor_number(self.state.cursor):
            logger.debug(
                "WeChat Native SSE 事件忽略：cursor=%s reason=cursor_already_committed", cursor
            )
            return
        data = event["data"]
        if event["kind"] == "message.recalled":
            thread, server_id, message_id = (
                data.get("threadId"),
                data.get("serverId"),
                data.get("messageId"),
            )
            if not isinstance(thread, str) or not thread:
                raise ValueError("invalid_recall_identity")
            if (
                not isinstance(server_id, str)
                or not server_id
                or (message_id is not None and not isinstance(message_id, str))
            ):
                raise ValueError("invalid_recall_identity")
            self.state.recall(thread, server_id, message_id)
            self.summary_cache.recall(thread, server_id)
            for active in list(self._events.values()):
                raw = active.message_obj.raw_message
                if raw["threadId"] == thread and (raw.get("serverId") == server_id):
                    active.stop_event()
                    media_task = active.get_extra("wechat_native_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
                elif (
                    raw["threadId"] == thread
                    and isinstance(raw.get("quote"), dict)
                    and raw["quote"].get("serverId") == server_id
                ):
                    active.set_extra("wechat_native_quote_invalidated", True)
                    active.stop_event()
                    if self.state.message_status(raw["id"]) == "queued":
                        self.state.mark_message(raw["id"], "no_reply")
                    media_task = active.get_extra("wechat_native_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
            self.state.advance(cursor)
            return
        if event["kind"] != "message.committed":
            self.state.advance(cursor)
            return
        message_id, thread = data.get("messageId"), data.get("threadId")
        logger.debug(
            "WeChat Native 收到消息事件：cursor=%s message=%s origin=%s",
            cursor,
            message_id,
            data.get("origin", "unknown"),
        )
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(thread, str)
            or not thread
        ):
            raise ValueError("invalid_message_identity")
        raw = await self.client.message(message_id)
        if (
            raw.get("id") != message_id
            or raw.get("accountId") != self.native_config.account_id
            or raw.get("threadId") != thread
        ):
            raise ValueError("message_identity_mismatch")
        origin = data.get("origin", "unknown")
        kind = self._classify(raw, origin)
        logger.debug(
            "WeChat Native 消息分类：message=%s type=%s app_type=%s direction=%s "
            "group=%s has_text=%s result=%s enable_media=%s enable_private_reply=%s",
            message_id,
            raw.get("type"),
            raw.get("appMessageType"),
            raw.get("direction"),
            thread.endswith("@chatroom"),
            isinstance(raw.get("body"), str) and bool(raw["body"].strip()),
            kind,
            self.native_config.enable_media,
            self.native_config.enable_private_reply,
        )
        eligible = kind != "ignore"
        cacheable = self.native_config.enable_group_summary and self._eligible(
            raw, origin, summary=True
        )
        converted = (
            convert_message(raw, self.native_config, self.bot_self_id)
            if eligible or cacheable
            else None
        )
        fresh = self.state.accept(
            cursor, message_id, thread, raw.get("serverId"), "queued" if eligible else "ignored"
        )
        if not fresh:
            logger.debug(
                "WeChat Native 消息去重跳过：message=%s status=%s",
                message_id,
                self.state.message_status(message_id),
            )
        if fresh and cacheable and not is_summary_command(converted.message_str):
            self.summary_cache.add(raw)
        if fresh and kind == "text":
            wrapped = WeChatNativeEvent(converted, self)
            try:
                self.commit_event(wrapped)
            except Exception:
                self.state.mark_message(message_id, "needs_review")
                logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                raise RuntimeError("Native event handoff requires inspection") from None
            self._events[message_id] = wrapped
            logger.debug("WeChat Native 文本已交付 AstrBot：message=%s", message_id)
        elif fresh and kind == "media":
            wrapped = WeChatNativeEvent(converted, self)
            self._events[message_id] = wrapped
            task = asyncio.create_task(self._process_media_message(raw, wrapped))
            wrapped.set_extra("wechat_native_media_task", task)
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)
            logger.debug(
                "WeChat Native 图片任务已调度：message=%s pending_tasks=%s",
                message_id,
                len(self._media_tasks),
            )

    async def run(self):
        self._run_task = asyncio.current_task()
        delay = self.reconnect_delay
        try:
            await self.client.open()
            while not self.stopped:
                try:
                    # Recheck on every connection, including initial transient failures.
                    account = await self.client.account()
                    if account.get("id") != self.native_config.account_id:
                        raise ValueError("account_identity_mismatch")
                    status = await self.client.status()
                    self.state.bind_server(status.get("serverId"))
                    self.bot_self_id = account.get("ownUsername") or self.native_config.account_id
                    if not account.get("authenticated") or account.get("connection") != "online":
                        logger.warning("WeChat Native 账号尚未在线；等待 Native 恢复连接")
                    async for event in self.client.events(self.state.cursor):
                        await self.process_native_event(event)
                        delay = self.reconnect_delay
                    if not self.stopped:
                        logger.warning("WeChat Native SSE 已断开，使用已保存游标重连")
                except NativeAPIError as error:
                    if error.status in {401, 403, 410}:
                        logger.error(
                            "WeChat Native 停止接收：HTTP %s (%s)；请检查认证或游标",
                            error.status,
                            error.code,
                        )
                        raise
                    logger.warning("WeChat Native 暂时不可达，将重连（HTTP %s）", error.status)
                except ValueError:
                    logger.error("WeChat Native 收到无效消息身份；保留游标，等待重试")
                if not self.stopped:
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.2))
                    delay = min(30, delay * 2)
        except asyncio.CancelledError:
            if not self.stopped:
                raise
        finally:
            await self.client.close()

    async def send_chain(self, thread: str, chain, *, source=None, event=None) -> bool:
        if self.stopped:
            raise RuntimeError("WeChat Native 已停止")
        task = asyncio.current_task()
        self._send_tasks.add(task)
        try:
            async with self._send_lock:
                if self.stopped:
                    raise RuntimeError("WeChat Native 已停止")
                if event and event.get_extra("wechat_native_quote_invalidated"):
                    event.stop_event()
                    return False
                text, mentions, reply_id = serialize_text(chain, group=thread.endswith("@chatroom"))
                if not text.strip():
                    return False
                if len(text.encode()) > 65536:
                    raise ValueError("Native 文本超过 64 KiB，未发送")
                if reply_id is not None and len(text.encode()) > 16384:
                    raise ValueError("Native 引用回复文本超过 16 KiB，未发送")
                if self.client.closed:
                    raise ValueError("Native 客户端未连接，未提交发送")
                message_id = source["id"] if source else None
                auto_mention = False
                if source:
                    source_status = self.state.message_status(message_id)
                    if source_status in {"needs_review", "failed", "partial"}:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if source_status == "recalled":
                        if event:
                            event.stop_event()
                        return False
                    if (
                        thread.endswith("@chatroom")
                        and mentions is None
                        and source_status != "replied"
                    ):
                        mentions = {"userIds": [source["senderId"]]}
                        auto_mention = True
                if mentions:
                    try:
                        await self.client.prepare_group_mentions(
                            thread, [self.bot_self_id, *mentions.get("userIds", [])]
                        )
                    except GroupMembersUnavailable as error:
                        if not auto_mention or error.code in {
                            "auth_required",
                            "account_identity_mismatch",
                        }:
                            raise
                        logger.warning("WeChat Native 省略自动 @，继续发送回复正文；%s", error)
                        mentions = None
                if source:
                    current = await self.client.message(message_id)
                    if (
                        current.get("id") != message_id
                        or current.get("accountId") != self.native_config.account_id
                        or current.get("threadId") != thread
                        or current.get("serverId") != source.get("serverId")
                    ):
                        raise ValueError("reply_source_identity_mismatch")
                    if self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if (
                        current.get("recall") is not None
                        or self.state.message_status(message_id) == "recalled"
                    ):
                        self.state.recall(thread, source.get("serverId"), message_id)
                        if event:
                            event.stop_event()
                        return False
                    original_quote = source.get("quote")
                    current_quote = current.get("quote")
                    if isinstance(original_quote, dict) and (
                        not isinstance(current_quote, dict)
                        or current_quote.get("serverId") != original_quote.get("serverId")
                        or (
                            original_quote.get("state") != "recalled"
                            and current_quote.get("state") == "recalled"
                        )
                        or (event and event.get_extra("wechat_native_quote_invalidated"))
                    ):
                        if source_status == "queued":
                            self.state.mark_message(message_id, "no_reply")
                        if event:
                            event.stop_event()
                        return False
                    original_id = (
                        event.get_extra("wechat_native_quote_original_id") if event else None
                    )
                    if original_id:
                        try:
                            await self._check_quote_original(source, event, original_id)
                        except (MediaError, NativeAPIError):
                            event.set_extra("wechat_native_quote_invalidated", True)
                            event.stop_event()
                            if self.state.message_status(message_id) == "queued":
                                self.state.mark_message(message_id, "no_reply")
                            return False
                quote = None
                if reply_id is not None:
                    target = (
                        current
                        if source and reply_id == message_id
                        else await self.client.message(reply_id)
                    )
                    if (
                        target.get("id") != reply_id
                        or target.get("accountId") != self.native_config.account_id
                        or target.get("threadId") != thread
                        or not isinstance(target.get("serverId"), str)
                        or not target["serverId"]
                        or target.get("recall") is not None
                        or self.state.message_status(reply_id) == "recalled"
                    ):
                        raise ValueError("Native 引用来源不可用、已撤回或不属于当前账号和聊天")
                    quote = {"messageId": reply_id, "serverId": target["serverId"]}
                    # Another target read may overlap source recall or send failure.
                    if source and self.state.message_status(message_id) == "recalled":
                        if event:
                            event.stop_event()
                        return False
                    if source and self.state.message_status(message_id) in {
                        "needs_review",
                        "failed",
                        "partial",
                    }:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                records = event.get_extra("wechat_native_summary_records") if event else None
                if event and event.get_extra("wechat_native_quote_invalidated"):
                    event.stop_event()
                    return False
                if records is not None and not self.summary_cache.contains(records):
                    self.state.mark_message(message_id, "no_reply")
                    event.stop_event()
                    return False
                key = "astrbot-" + uuid.uuid4().hex
                self.state.begin_send(key, message_id, thread)
                logger.debug(
                    "WeChat Native 开始发送回复：message=%s receipt_key=%s", message_id, key
                )
                try:
                    receipt = await self.client.send_text(thread, text, key, mentions, quote=quote)
                except BaseException as error:
                    state = "needs_review"
                    if isinstance(error, SendError) and error.state in {"failed", "cancelled"}:
                        state = error.state
                    elif isinstance(error, NativeAPIError) and 400 <= error.status < 500:
                        state = "failed"
                    send_id = error.send_id if isinstance(error, SendError) else None
                    self.state.finish_send(key, state, send_id)
                    if message_id:
                        self.state.mark_message(
                            message_id, "failed" if state == "cancelled" else state
                        )
                    logger.error(
                        "WeChat Native 回复未确认；状态 %s，回执键 %s。请检查状态库，勿盲目重发",
                        state,
                        key,
                    )
                    raise
                self.state.finish_send(key, "accepted", receipt["id"])
                if message_id:
                    self.state.mark_message(message_id, "replied")
                logger.info(
                    "WeChat Native 回复已被 Native 接受：message=%s send=%s",
                    message_id,
                    receipt["id"],
                )
                return True
        finally:
            self._send_tasks.discard(task)

    async def send_by_session(self, session, message_chain):
        thread = session_target(session.session_id, self.native_config.account_id)
        if session.platform_id != self.meta().id:
            raise ValueError("会话不属于当前机器人实例")
        if await self.send_chain(thread, message_chain):
            await super().send_by_session(session, message_chain)

    async def terminate(self):
        async with self._terminate_lock:
            await self._terminate()

    async def _terminate(self):
        if self._state_closed:
            return
        self.stopped = True
        current = asyncio.current_task()
        tasks = {task for task in (self._send_tasks | self._media_tasks) if task is not current}
        if self._run_task and self._run_task is not current:
            tasks.add(self._run_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in list(self._events.values()):
            event.stop_event()
        self.summary_cache.clear()
        await self.client.close()
        self.state.close()
        self._state_closed = True
