"""Registered in-process AstrBot platform adapter."""

import asyncio
import hashlib
import random
import time
import uuid
import weakref
from pathlib import Path

from astrbot.api import logger
from astrbot.api.platform import Platform, PlatformMetadata, register_platform_adapter
from astrbot.api.star import StarTools

from .config import ADAPTER_ID, CONFIG_METADATA, DEFAULT_CONFIG, PLUGIN_ID, NativeConfig
from .event import (
    WeChatNativeEvent,
    convert_message,
    message_timestamp,
    serialize_text,
    session_target,
)
from .native_client import NativeAPIError, NativeClient, SendError
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

    def _eligible(self, raw: dict, origin: str, *, summary: bool = False) -> bool:
        cfg = self.native_config
        stamp = message_timestamp(raw)
        now = time.time()
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
        thread = raw["threadId"]
        if not thread.endswith("@chatroom"):
            return (
                not summary
                and cfg.enable_private_reply
                and (not cfg.allowed_users or raw["senderId"] in cfg.allowed_users)
            )
        if cfg.group_reply_mode == "disabled" or (
            cfg.allowed_groups and thread not in cfg.allowed_groups
        ):
            return False
        if summary:
            return cfg.enable_group_summary
        if cfg.group_reply_mode == "all":
            return True
        mentions = raw.get("mentions")
        return (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        )

    async def process_native_event(self, event: dict):
        if self.stopped:
            return
        cursor = event["cursor"]
        if cursor_number(cursor) <= cursor_number(self.state.cursor):
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
                elif (
                    raw["threadId"] == thread
                    and isinstance(raw.get("quote"), dict)
                    and raw["quote"].get("serverId") == server_id
                ):
                    active.set_extra("wechat_native_quote_invalidated", True)
                    active.stop_event()
                    if self.state.message_status(raw["id"]) == "queued":
                        self.state.mark_message(raw["id"], "no_reply")
            self.state.advance(cursor)
            return
        if event["kind"] != "message.committed":
            self.state.advance(cursor)
            return
        message_id, thread = data.get("messageId"), data.get("threadId")
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
        eligible = self._eligible(raw, data.get("origin", "unknown"))
        cacheable = self.native_config.enable_group_summary and self._eligible(
            raw, data.get("origin", "unknown"), summary=True
        )
        converted = (
            convert_message(raw, self.native_config, self.bot_self_id)
            if eligible or cacheable
            else None
        )
        fresh = self.state.accept(
            cursor, message_id, thread, raw.get("serverId"), "queued" if eligible else "ignored"
        )
        if fresh and cacheable and not is_summary_command(converted.message_str):
            self.summary_cache.add(raw)
        if fresh and eligible:
            wrapped = WeChatNativeEvent(converted, self)
            try:
                self.commit_event(wrapped)
            except Exception:
                self.state.mark_message(message_id, "needs_review")
                logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                raise RuntimeError("Native event handoff requires inspection") from None
            self._events[message_id] = wrapped

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
                if source:
                    source_status = self.state.message_status(message_id)
                    if source_status in {"needs_review", "failed", "partial"}:
                        raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
                    if source_status == "recalled":
                        if event:
                            event.stop_event()
                        return False
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
                    if (
                        thread.endswith("@chatroom")
                        and mentions is None
                        and source_status != "replied"
                    ):
                        mentions = {"userIds": [source["senderId"]]}
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
        tasks = {task for task in self._send_tasks if task is not current}
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
