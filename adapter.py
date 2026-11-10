"""Registered in-process AstrBot platform adapter."""

import asyncio
import random
import re
import time
import uuid
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Record
from astrbot.api.platform import (
    AstrBotMessage,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.api.star import StarTools

from .config import (
    ADAPTER_ID,
    CONFIG_METADATA,
    DEFAULT_CONFIG,
    MAX_CONCURRENT_MEDIA_TASKS,
    MAX_RECENT_GROUP_IMAGES,
    MAX_RECENT_GROUP_PDFS,
    MAX_RECENT_GROUP_VOICES,
    PLUGIN_ID,
    RECENT_GROUP_IMAGE_SECONDS,
    RECENT_GROUP_PDF_SECONDS,
    RECENT_GROUP_VOICE_SECONDS,
    NativeConfig,
)
from .event import (
    WeChatNativeEvent,
    convert_message,
    message_timestamp,
    serialize_chain,
    session_target,
)
from .history_tools import HistoryTools
from .loop_guard import LoopGuard
from .native_client import (
    GroupMembersUnavailable,
    MediaError,
    NativeAPIError,
    NativeClient,
    QuotedMediaMetadataError,
    SendError,
    extract_pdf_text,
    is_pdf_filename,
    load_outgoing_image,
    load_outgoing_voice,
)
from .pat import pat_actor, reference_current
from .pat import prepare as prepare_pat
from .state import StateStore, cursor_number, state_path
from .summary import GroupSummaryCache, is_summary_command
from .wechat_actions import WeChatActions

RED_PACKET_COOLDOWN = 30
# A deployed Native instance reported a confirmed group red packet as app subtype 47.
RED_PACKET_APP_TYPES = (47, 2001)
RED_PACKET_TEMPLATES = (
    "检测到财富气息，大家手速准备！💰",
    "感谢老板！{nickname}宣布你是今天最帅的人！😎",
    "{nickname}的小雷达响了！是哪位好心人掉了一地好运呀？📡",
    "潜水员集合！水面上漂来一只红包啦～🫧",
    "{nickname}开个竞猜，今天谁是手气王？👑",
    "咚咚咚！给发红包的老板敲三下小鼓！🥁",
    "抢到了来报个喜，没抢到来跟{nickname}击个掌！✋",
    "{nickname}把好运摇匀啦，来看看谁先抽到！🧧",
)


@dataclass
class RedPacketReplies:
    last_attempt: float = float("-inf")
    last_template: str | None = None
    remaining: list[str] = field(default_factory=list)

    def next_template(self, nickname: str) -> str:
        def available(template: str) -> bool:
            return bool(nickname) or "{nickname}" not in template

        for index, template in enumerate(self.remaining):
            if available(template):
                return self.remaining.pop(index)
        self.remaining = list(RED_PACKET_TEMPLATES)
        random.shuffle(self.remaining)
        index = next(
            i
            for i, template in enumerate(self.remaining)
            if template != self.last_template and available(template)
        )
        return self.remaining.pop(index)


@register_platform_adapter(
    ADAPTER_ID,
    "直接连接 agent-wechat-native 的个人微信平台适配器",
    default_config_tmpl=DEFAULT_CONFIG.copy(),
    adapter_display_name="WeChat Native",
    logo_path="logo.png",
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
        self.admin_client = None
        if cfg.enable_wechat_actions and cfg.admin_token_file:
            admin_token = replace(cfg, token="", token_file=cfg.admin_token_file).load_token()
            self.admin_client = NativeClient(cfg.api_url, cfg.account_id, admin_token)
        # PlatformManager owns client_self_id (its instance/task UUID).
        self.bot_self_id = cfg.account_id
        root = state_dir if state_dir is not None else StarTools.get_data_dir(PLUGIN_ID)
        self.state = StateStore(
            state_path(root, cfg.api_url, cfg.account_id, platform_config.get("id", ADAPTER_ID))
        )
        self.stopped = False
        self._state_closed = False
        self._run_task = None
        self._send_tasks = set()
        self._media_tasks = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA_TASKS)
        self._recent_group_images = OrderedDict()
        self._recent_group_pdfs = OrderedDict()
        self._recent_group_voices = OrderedDict()
        self._red_packet_replies: dict[str, RedPacketReplies] = {}
        self._pat_attempts: dict[str, float] = {}
        self._events = weakref.WeakValueDictionary()
        self._send_lock = asyncio.Lock()
        self.loop_guard = LoopGuard(self.state, cfg.account_id)
        self.history = HistoryTools(self)
        self.actions = WeChatActions(self)
        self._terminate_lock = asyncio.Lock()
        self._last_time_warning = float("-inf")
        self.summary_cache = GroupSummaryCache(cfg.summary_max_messages, cfg.summary_window_minutes)
        logger.info(
            "WeChat Native 运行时工具开关："
            "history_tools=%s wechat_actions=%s admin_token_loaded=%s pat_reply=%s",
            cfg.enable_history_tools,
            cfg.enable_wechat_actions,
            self.admin_client is not None,
            cfg.enable_group_pat_reply,
        )

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=ADAPTER_ID,
            description="WeChat Native",
            id=self.config.get("id", ADAPTER_ID),
            adapter_display_name="WeChat Native",
            support_streaming_message=False,
        )

    def create_event(self, message: AstrBotMessage) -> WeChatNativeEvent:
        # Debounce and other plugins re-inject messages through this factory.
        # The base Platform.create_event returns a generic event whose send()
        # only records metrics, so replies to re-injected messages silently
        # never reach WeChat.
        event = WeChatNativeEvent(message, self)
        original = self._events.get(message.message_id)
        if (
            original is not None
            and isinstance(message.raw_message, dict)
            and message.raw_message == original.message_obj.raw_message
        ):
            # Preserve trusted event ownership across framework re-injection.
            self._events[message.message_id] = event
        return event

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
        if raw.get("pat") is not None:
            thread = raw["threadId"]
            if (
                not cfg.enable_group_pat_reply
                or not pat_actor(raw)
                or raw["pat"].get("targetId") != self.bot_self_id
                or not thread.endswith("@chatroom")
                or cfg.group_reply_mode == "disabled"
                or (cfg.allowed_groups and thread not in cfg.allowed_groups)
            ):
                return ignored("pat_not_allowed")
            return "pat" if self._check_timestamp(raw) else ignored("invalid_timestamp")
        msg_type = raw.get("type")
        if msg_type == 49 and raw.get("appMessageType") in RED_PACKET_APP_TYPES:
            thread = raw["threadId"]
            if (
                not cfg.enable_group_red_packet_reply
                or not thread.endswith("@chatroom")
                or cfg.group_reply_mode == "disabled"
                or (cfg.allowed_groups and thread not in cfg.allowed_groups)
            ):
                return ignored("red_packet_reply_disabled")
            return "red_packet" if self._check_timestamp(raw) else ignored("invalid_timestamp")
        is_direct_image = msg_type == 3
        is_direct_voice = msg_type == 34
        # Native exposes a file's name only through its media asset, so a direct
        # file is only a candidate here; non-PDF files are dropped after that lookup.
        is_direct_file = msg_type == 49 and raw.get("appMessageType") == 6
        is_quote = msg_type == 49 and raw.get("appMessageType") == 57
        quote = raw.get("quote")
        is_quoted_image = is_quote and isinstance(quote, dict) and quote.get("type") == 3
        is_quoted_voice = is_quote and isinstance(quote, dict) and quote.get("type") == 34
        # Native reports the quoted file's title as embeddedText (state is "unsupported").
        is_quoted_pdf = (
            is_quote
            and isinstance(quote, dict)
            and quote.get("type") == 49
            and quote.get("appMessageType") == 6
            and is_pdf_filename(quote.get("embeddedText"))
        )
        body = raw.get("body")
        has_text_body = isinstance(body, str) and bool(body.strip())
        if is_direct_image or is_direct_voice:
            if not cfg.enable_media:
                return ignored("media_disabled")
            if body is not None and not isinstance(body, str):
                return ignored("invalid_image_body")
        elif is_direct_file:
            if not cfg.enable_media:
                return ignored("media_disabled")
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
            if cfg.enable_media and (
                is_direct_image
                or is_quoted_image
                or is_direct_file
                or is_quoted_pdf
                or is_direct_voice
                or is_quoted_voice
            ):
                return "media"
            return "text" if has_text_body else ignored("empty_text_body")
        if cfg.group_reply_mode == "disabled":
            return ignored("group_reply_disabled")
        if cfg.allowed_groups and thread not in cfg.allowed_groups:
            return ignored("group_not_allowed")
        if is_direct_image:
            return ignored("direct_group_image")
        if is_direct_file:
            return ignored("direct_group_file")
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
        if is_direct_voice:
            return (
                "media"
                if cfg.group_reply_mode in {"all", "probability"} or mentioned
                else ignored("voice_without_mention")
            )
        if cfg.enable_media and (is_quoted_image or is_quoted_pdf or is_quoted_voice):
            return "media" if mentioned else ignored("quoted_media_without_mention")
        if cfg.group_reply_mode in {"all", "probability"} or mentioned:
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

    def _remember_group_image(self, raw: dict, origin: str) -> None:
        """Remember a group member's latest media ID for a following @ request."""
        cfg = self.native_config
        thread, sender = raw["threadId"], raw.get("senderId")
        if raw.get("type") == 3:
            store, maximum = self._recent_group_images, MAX_RECENT_GROUP_IMAGES
            seconds = RECENT_GROUP_IMAGE_SECONDS
        elif raw.get("type") == 34:
            store, maximum = self._recent_group_voices, MAX_RECENT_GROUP_VOICES
            seconds = RECENT_GROUP_VOICE_SECONDS
        elif raw.get("type") == 49 and raw.get("appMessageType") == 6:
            store, maximum = self._recent_group_pdfs, MAX_RECENT_GROUP_PDFS
            seconds = RECENT_GROUP_PDF_SECONDS
        else:
            return
        if (
            not cfg.enable_media
            or cfg.group_reply_mode == "disabled"
            or not thread.endswith("@chatroom")
            or origin != "live"
            or raw.get("direction") != "incoming"
            or raw.get("recall") is not None
            or not isinstance(sender, str)
            or not sender
            or sender == self.bot_self_id
            or (cfg.allowed_groups and thread not in cfg.allowed_groups)
            or not isinstance(raw.get("serverId"), str)
            or not raw["serverId"]
        ):
            return
        stamp = message_timestamp(raw)
        now = time.time()
        if stamp is None or not -60 <= now - stamp <= seconds:
            return
        key = (thread, sender)
        previous = store.get(key)
        if previous and previous["timestamp"] > stamp:
            return
        store[key] = {
            "serverId": raw["serverId"],
            "senderId": sender,
            "timestamp": min(stamp, now),
        }
        store.move_to_end(key)
        while len(store) > maximum:
            store.popitem(last=False)

    def _recent_media_request(self, raw: dict, prompt: str) -> tuple[str | None, dict | None]:
        """Return the requested media kind and same-sender recent source for an @ text."""
        mentions = raw.get("mentions")
        if (
            not self.native_config.enable_media
            or raw.get("type") != 1
            or not raw["threadId"].endswith("@chatroom")
            or not isinstance(mentions, dict)
            or mentions.get("state") != "known"
            or mentions.get("self") is not True
            or is_summary_command(prompt)
        ):
            return None, None
        key = (raw["threadId"], raw["senderId"])
        sources = {}
        for name, store, seconds in (
            ("image", self._recent_group_images, RECENT_GROUP_IMAGE_SECONDS),
            ("pdf", self._recent_group_pdfs, RECENT_GROUP_PDF_SECONDS),
            ("voice", self._recent_group_voices, RECENT_GROUP_VOICE_SECONDS),
        ):
            source = store.get(key)
            if source and time.time() - source["timestamp"] > seconds:
                store.pop(key)
                source = None
            sources[name] = source
        image_asked = bool(
            re.search(r"图片|照片|截图|看图|识图|这张图|那张图|刚才.{0,6}图", prompt)
        )
        # Generic file words only count when a recent file exists, so ordinary chat
        # mentioning "文档" is never hijacked; an explicit "PDF" always counts.
        file_asked = bool(re.search(r"(?i)pdf", prompt)) or (
            sources["pdf"] is not None and bool(re.search(r"文件|文档|论文|这份|那份", prompt))
        )
        voice_asked = bool(
            re.search(r"语音|录音|音频|听听|听一下|转文字|转成文字|说了啥|说了什么", prompt)
        )
        wanted = [
            name
            for name, asked in (("image", image_asked), ("pdf", file_asked), ("voice", voice_asked))
            if asked
        ]
        if not wanted:
            present = [name for name, source in sources.items() if source]
            if prompt.strip() or not present:
                return None, None
            wanted = [max(present, key=lambda name: sources[name]["timestamp"])]
        elif len(wanted) > 1:
            present = [name for name in wanted if sources[name]]
            wanted = (
                [max(present, key=lambda name: sources[name]["timestamp"])]
                if present
                else wanted[:1]
            )
        return wanted[0], sources[wanted[0]]

    async def _check_quote_original(
        self, raw: dict, wrapped: WeChatNativeEvent, original_id: str
    ) -> None:
        media_kind = wrapped.get_extra("wechat_native_media_kind") or "image"
        pdf = media_kind == "pdf"
        quote = wrapped.get_extra("wechat_native_recent_image") or raw["quote"]
        original = await self.client.message(original_id)
        if (
            original.get("id") != original_id
            or original.get("accountId") != self.native_config.account_id
            or original.get("threadId") != raw["threadId"]
            or original.get("serverId") != quote["serverId"]
            or original.get("senderId") != quote["senderId"]
            or original.get("type") != {"pdf": 49, "image": 3, "voice": 34}[media_kind]
            or (pdf and original.get("appMessageType") != 6)
        ):
            raise MediaError("unavailable", media_kind=media_kind)
        if (
            original.get("recall") is not None
            or self.state.message_status(original_id) == "recalled"
        ):
            wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise MediaError("recalled")

    async def _fetch_quote_original(
        self, raw: dict, wrapped: WeChatNativeEvent, guard, media_kind: str = "image"
    ):
        """Fetch the same-chat original of a quote or recent request.

        Returns image bytes, or ``(pdf_bytes, filename)`` when ``media_kind`` is "pdf".
        """
        pdf = media_kind == "pdf"

        def fail(reason: str) -> MediaError:
            return MediaError(reason, media_kind=media_kind)

        recent = wrapped.get_extra("wechat_native_recent_image")
        quote = recent or raw["quote"]
        if not all(
            isinstance(quote.get(key), str) and quote[key] for key in ("serverId", "senderId")
        ):
            raise fail("unavailable")
        original_id = self.state.find_message_id(raw["threadId"], quote["serverId"])
        if original_id is None:
            raise fail("unavailable" if recent or media_kind == "image" else "expired")
        try:
            guard()
            await self._check_quote_original(raw, wrapped, original_id)
            guard()
            if not recent and media_kind == "image":
                logger.warning(
                    "WeChat Native 引用媒体元数据无效，回退同聊天原图：message=%s original=%s",
                    raw["id"],
                    original_id,
                )
            if media_kind == "voice":
                payload = await self.client.fetch_voice(original_id, guard=guard)
            elif pdf:
                payload = await self.client.fetch_pdf(original_id, guard=guard)
            else:
                payload = await self.client.fetch_image(original_id, guard=guard)
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
                raise fail("recalled") from None
            raise fail("missing_scope" if error.status in {401, 403} else "unavailable") from None
        if any(
            current.get(key) != raw.get(key) for key in ("id", "accountId", "threadId", "serverId")
        ):
            raise fail("unavailable")
        if current.get("recall") is not None:
            raise fail("recalled")
        current_quote = current.get("quote")
        quote_changed = (
            current.get("type") != 49
            or current.get("appMessageType") != 57
            or not isinstance(current_quote, dict)
            or any(
                current_quote.get(key) != quote.get(key) for key in ("serverId", "senderId", "type")
            )
            or current_quote.get("state") == "recalled"
        )
        if media_kind == "image":
            # Native embeds the image itself; a quote of a file is always "unsupported".
            quote_changed = (
                quote_changed
                or current_quote.get("state") != "embedded"
                or current_quote.get("embeddedImage") is not True
            )
        if not recent and quote_changed:
            wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise fail("recalled")
        if recent and (current.get("type") != 1 or current.get("senderId") != raw["senderId"]):
            wrapped.set_extra("wechat_native_quote_invalidated", True)
            raise fail("recalled")
        wrapped.set_extra("wechat_native_quote_original_id", original_id)
        return payload

    async def _process_media_message(self, raw: dict, wrapped: WeChatNativeEvent) -> None:
        task = asyncio.current_task()
        message_id = raw["id"]
        thread = raw["threadId"]
        is_quote = raw.get("type") == 49 and raw.get("appMessageType") == 57
        media_kind = wrapped.get_extra("wechat_native_media_kind") or "image"
        is_pdf = media_kind == "pdf"
        is_voice = media_kind == "voice"
        label = {"pdf": "PDF", "voice": "语音", "image": "图片"}[media_kind]
        recent_request = wrapped.get_extra("wechat_native_recent_image_request")

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
                    "WeChat Native %s处理开始：message=%s quoted=%s", label, message_id, is_quote
                )
                if recent_request and not wrapped.get_extra("wechat_native_recent_image"):
                    raise MediaError("recent_unavailable", media_kind=media_kind)
                if is_quote:
                    quote = raw.get("quote")
                    if (
                        not isinstance(quote, dict)
                        or quote.get("type") != {"pdf": 49, "voice": 34, "image": 3}[media_kind]
                    ):
                        raise MediaError("unavailable", media_kind=media_kind)
                    state = quote.get("state")
                    if media_kind == "image":
                        logger.debug(
                            "WeChat Native 引用图片检查：message=%s state=%s embedded_image=%s",
                            message_id,
                            state,
                            quote.get("embeddedImage") is True,
                        )
                    else:
                        logger.debug(
                            "WeChat Native 引用%s检查：message=%s state=%s",
                            label,
                            message_id,
                            state,
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
                    if media_kind == "image" and state == "unsupported":
                        raise MediaError("unsupported_format")
                    if media_kind == "image" and (
                        state != "embedded" or quote.get("embeddedImage") is not True
                    ):
                        raise MediaError("expired")
                image_bytes = pdf_name = pdf_text = voice_bytes = None
                try:
                    async with asyncio.timeout(self.client.media_timeout):
                        if is_voice:
                            if is_quote or recent_request:
                                voice_bytes = await self._fetch_quote_original(
                                    raw, wrapped, guard, "voice"
                                )
                            else:
                                voice_bytes = await self.client.fetch_voice(message_id, guard=guard)
                            latest = await self.client.message(message_id)
                            guard()
                            if any(
                                latest.get(key) != raw.get(key)
                                for key in ("id", "accountId", "threadId", "serverId")
                            ):
                                raise MediaError("unavailable", media_kind="voice")
                            if latest.get("recall") is not None:
                                raise MediaError("recalled", media_kind="voice")
                        elif is_pdf:
                            if is_quote or recent_request:
                                pdf_data, pdf_name = await self._fetch_quote_original(
                                    raw, wrapped, guard, "pdf"
                                )
                            else:
                                pdf_data, pdf_name = await self.client.fetch_pdf(
                                    message_id, guard=guard
                                )
                            guard()
                            # Parsing is CPU-bound; keep the event loop responsive.
                            pdf_text = await asyncio.to_thread(extract_pdf_text, pdf_data)
                            del pdf_data
                        else:
                            try:
                                if recent_request:
                                    image_bytes = await self._fetch_quote_original(
                                        raw, wrapped, guard
                                    )
                                else:
                                    image_bytes = await self.client.fetch_image(
                                        message_id, quoted=is_quote, guard=guard
                                    )
                            except QuotedMediaMetadataError:
                                image_bytes = await self._fetch_quote_original(raw, wrapped, guard)
                except TimeoutError:
                    raise MediaError("unavailable", media_kind=media_kind) from None
                guard()
                payload_size = (
                    len(pdf_text) if is_pdf else len(voice_bytes if is_voice else image_bytes)
                )
                logger.debug(
                    "WeChat Native %s处理完成：message=%s size=%s", label, message_id, payload_size
                )
                enriched = convert_message(
                    raw,
                    self.native_config,
                    self.bot_self_id,
                    image_bytes=image_bytes,
                    pdf_name=pdf_name,
                    voice_bytes=voice_bytes,
                )
                event = WeChatNativeEvent(enriched, self)
                event.set_extra("wechat_native_media_kind", media_kind)
                if is_pdf:
                    event.set_extra("wechat_native_pdf", {"name": pdf_name, "text": pdf_text})
                original_id = wrapped.get_extra("wechat_native_quote_original_id")
                if original_id:
                    event.set_extra("wechat_native_quote_original_id", original_id)
                if recent_request:
                    event.set_extra(
                        "wechat_native_recent_image",
                        wrapped.get_extra("wechat_native_recent_image"),
                    )
                try:
                    self.commit_event(event)
                except Exception:
                    self.state.mark_message(message_id, "needs_review")
                    logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                    raise RuntimeError("Native event handoff requires inspection") from None
                self._events[message_id] = event
                logger.info(
                    "WeChat Native %s已交付 AstrBot：message=%s size=%s components=%s",
                    label,
                    message_id,
                    payload_size,
                    len(enriched.message),
                )
        except MediaError as error:
            log = logger.debug if error.reason in {"recalled", "not_pdf"} else logger.warning
            log(
                "WeChat Native %s处理终止：message=%s reason=%s",
                label,
                message_id,
                error.reason,
            )
            if error.reason == "not_pdf":
                # Only PDFs are handled; any other file is dropped without a reply. A
                # text request that merely matched a recent non-PDF file stays ordinary chat.
                if (
                    recent_request
                    and not self.stopped
                    and not self._state_closed
                    and not wrapped.is_stopped()
                    and self.state.message_status(message_id) == "queued"
                ):
                    try:
                        self.commit_event(wrapped)
                    except Exception:
                        self.state.mark_message(message_id, "needs_review")
                        logger.error("WeChat Native 队列交付失败；消息标识已保留，需人工检查")
                    return
                if not self._state_closed and self.state.message_status(message_id) == "queued":
                    self.state.mark_message(message_id, "no_reply")
                return
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
                    "WeChat Native %s错误提示已抑制：message=%s reason=event_invalidated",
                    label,
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
                    "WeChat Native %s错误提示发送结果：message=%s sent=%s", label, message_id, sent
                )
                if (
                    not sent
                    and not self._state_closed
                    and self.state.message_status(message_id) == "queued"
                ):
                    self.state.mark_message(message_id, "no_reply")
            except (Exception, asyncio.CancelledError) as send_err:
                logger.error(
                    "WeChat Native %s错误提示发送失败：message=%s error_type=%s",
                    label,
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
                "WeChat Native %s任务取消：message=%s stopped=%s", label, message_id, self.stopped
            )
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            if self.stopped:
                raise
        except Exception as error:
            if not self._state_closed and self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "needs_review")
            logger.error(
                "WeChat Native %s处理失败：message=%s error_type=%s；消息标识已保留，需人工检查",
                label,
                message_id,
                type(error).__name__,
            )
        finally:
            self._media_tasks.discard(task)

    async def _reply_to_red_packet(self, raw: dict) -> None:
        thread, message_id = raw["threadId"], raw["id"]
        replies = self._red_packet_replies.setdefault(thread, RedPacketReplies())
        if time.monotonic() - replies.last_attempt < RED_PACKET_COOLDOWN:
            self.state.mark_message(message_id, "no_reply")
            return

        nickname = ""
        try:
            group = await self.client.group(thread)
            account = self.native_config.account_id
            if group.get("roomId") == thread and group.get("accountId", account) == account:
                value = group.get("myDisplayName")
                if isinstance(value, str):
                    nickname = " ".join(value.split())[:128]
        except NativeAPIError:
            pass

        template = replies.next_template(nickname)
        chain = MessageChain([Plain(template.format(nickname=nickname))])
        try:
            sent = await self._send_chain(thread, chain, source=raw, red_packet=True)
        except (NativeAPIError, SendError, ValueError, RuntimeError) as error:
            if self.state.message_status(message_id) == "queued":
                self.state.mark_message(message_id, "failed")
            reason = error.code if isinstance(error, NativeAPIError) else type(error).__name__
            logger.warning("WeChat Native 红包暖场发送失败：%s", reason)
        else:
            if not sent:
                replies.remaining.insert(0, template)
                self.state.mark_message(message_id, "no_reply")
                return
            logger.info("WeChat Native 红包暖场已发送")
        replies.last_attempt = time.monotonic()
        replies.last_template = template

    async def process_native_event(self, event: dict):
        # Recall delivery must remain able to invalidate a reply during media I/O.
        if event.get("kind") == "message.recalled":
            await self._process_native_event(event)
            return
        thread = event.get("data", {}).get("threadId", "")
        async with self.loop_guard.gate(thread if isinstance(thread, str) else ""):
            await self._process_native_event(event)

    async def _process_native_event(self, event: dict):
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
            for store in (
                self._recent_group_images,
                self._recent_group_pdfs,
                self._recent_group_voices,
            ):
                for key, source in list(store.items()):
                    if key[0] == thread and source["serverId"] == server_id:
                        store.pop(key)
            for active in list(self._events.values()):
                raw = active.message_obj.raw_message
                reference = (
                    active.get_extra("wechat_native_pat_reference")
                    or active.get_extra("wechat_native_recent_image")
                    or raw.get("quote")
                )
                if raw["threadId"] == thread and (raw.get("serverId") == server_id):
                    active.stop_event()
                    media_task = active.get_extra("wechat_native_media_task")
                    if media_task and not media_task.done():
                        media_task.cancel()
                elif (
                    raw["threadId"] == thread
                    and isinstance(reference, dict)
                    and reference.get("serverId") == server_id
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
        received = (
            origin == "live"
            and raw.get("direction") == "incoming"
            and raw.get("senderId") != self.bot_self_id
        )
        if self.loop_guard.blocked(thread):
            self.state.accept(
                cursor, message_id, thread, raw.get("serverId"), "ignored", received=received
            )
            return
        kind = self._classify(raw, origin)
        if raw.get("pat") is not None:
            logger.info("WeChat Native 拍一拍准入：message=%s result=%s", message_id, kind)
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
        if kind == "pat":
            if not self.state.accept(
                cursor, message_id, thread, raw.get("serverId"), "queued", received=received
            ):
                return
            now = time.monotonic()
            if now - self._pat_attempts.get(thread, float("-inf")) < 30:
                self.state.mark_message(message_id, "no_reply")
                logger.info("WeChat Native 拍一拍冷却跳过：message=%s", message_id)
                return
            self._pat_attempts[thread] = now
            wrapped = WeChatNativeEvent(
                convert_message(raw, self.native_config, self.bot_self_id), self
            )
            self._events[message_id] = wrapped
            task = asyncio.create_task(prepare_pat(self, raw, wrapped))
            wrapped.set_extra("wechat_native_media_task", task)
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)
            return
        if kind == "red_packet":
            if self.state.accept(
                cursor, message_id, thread, raw.get("serverId"), "queued", received=received
            ):
                await self._reply_to_red_packet(raw)
            return
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
            cursor,
            message_id,
            thread,
            raw.get("serverId"),
            "queued" if eligible else "ignored",
            received=received,
        )
        recent_kind, recent_source = None, None
        if fresh:
            self._remember_group_image(raw, origin)
            if kind == "text":
                recent_kind, recent_source = self._recent_media_request(raw, converted.message_str)
                if recent_kind:
                    kind = "media"
        if not fresh:
            logger.debug(
                "WeChat Native 消息去重跳过：message=%s status=%s",
                message_id,
                self.state.message_status(message_id),
            )
        if fresh and cacheable and not is_summary_command(converted.message_str):
            self.summary_cache.add(raw)
        if fresh and kind == "text":
            self.loop_guard.incoming(thread, message_id, converted.message_str)
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
            self.loop_guard.incoming(thread, message_id, converted.message_str)
            wrapped = WeChatNativeEvent(converted, self)
            quote = raw.get("quote")
            wrapped.set_extra(
                "wechat_native_media_kind",
                recent_kind
                or (
                    "voice"
                    if raw.get("type") == 34
                    or (isinstance(quote, dict) and quote.get("type") == 34)
                    else "pdf"
                    if raw.get("type") == 49
                    and (
                        raw.get("appMessageType") == 6
                        or (isinstance(quote, dict) and quote.get("type") == 49)
                    )
                    else "image"
                ),
            )
            if recent_kind:
                wrapped.set_extra("wechat_native_recent_image_request", True)
                wrapped.set_extra("wechat_native_recent_image", recent_source)
            self._events[message_id] = wrapped
            task = asyncio.create_task(self._process_media_message(raw, wrapped))
            wrapped.set_extra("wechat_native_media_task", task)
            self._media_tasks.add(task)
            task.add_done_callback(self._media_tasks.discard)
            media_label = {"pdf": "PDF", "voice": "语音", "image": "图片"}.get(
                wrapped.get_extra("wechat_native_media_kind"), "媒体"
            )
            logger.debug(
                "WeChat Native %s任务已调度：message=%s pending_tasks=%s",
                media_label,
                message_id,
                len(self._media_tasks),
            )

    async def run(self):
        self._run_task = asyncio.current_task()
        delay = self.reconnect_delay
        try:
            await self.client.open()
            recovery = None
            while not self.stopped:
                try:
                    # Recheck on every connection, including initial transient failures.
                    account = await self.client.account()
                    if account.get("id") != self.native_config.account_id:
                        raise ValueError("account_identity_mismatch")
                    status = await self.client.status()
                    self.state.bind_server(status.get("serverId"))
                    self.bot_self_id = account.get("ownUsername") or self.native_config.account_id
                    if recovery is None:
                        recovery = asyncio.create_task(self.actions.recover())
                        self.actions.tasks.add(recovery)
                        recovery.add_done_callback(self.actions.tasks.discard)
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
            if self.admin_client:
                await self.admin_client.close()

    async def send_chain(self, thread: str, chain, *, source=None, event=None) -> bool:
        if self.stopped:
            raise RuntimeError("WeChat Native 已停止")
        task = asyncio.current_task()
        self._send_tasks.add(task)
        try:
            async with self.loop_guard.gate(thread):
                if self.stopped:
                    raise RuntimeError("WeChat Native 已停止")
                if self.loop_guard.blocked(thread):
                    if event:
                        event.stop_event()
                    return False
                return await self._send_chain(thread, chain, source=source, event=event)
        finally:
            self._send_tasks.discard(task)

    async def _send_chain(
        self, thread: str, chain, *, source=None, event=None, red_packet: bool = False
    ) -> bool:
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
                parts, mentions, reply_id = serialize_chain(
                    chain, group=thread.endswith("@chatroom")
                )
                if not parts:
                    return False
                has_text = any(isinstance(part, str) for part in parts)
                if (
                    source
                    and not red_packet
                    and not (event and event.get_extra("wechat_native_plain_reply"))
                    and thread.endswith("@chatroom")
                    and has_text
                    and self.native_config.group_reply_style == "quote"
                    and reply_id is None
                    and self.state.message_status(source["id"]) != "replied"
                ):
                    reply_id = source["id"]
                for part in parts:
                    if isinstance(part, str) and len(part.encode()) > 65536:
                        raise ValueError("Native 文本超过 64 KiB，未发送")
                    if (
                        isinstance(part, str)
                        and reply_id is not None
                        and len(part.encode()) > 16384
                    ):
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
                        not red_packet
                        and not (event and event.get_extra("wechat_native_plain_reply"))
                        and thread.endswith("@chatroom")
                        and self.native_config.group_reply_style == "mention"
                        and mentions is None
                        and has_text
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
                    if source.get("pat") is not None and event:
                        reference = event.get_extra("wechat_native_pat_reference")
                        if reference and not await reference_current(self, reference):
                            self.state.mark_message(message_id, "no_reply")
                            event.stop_event()
                            return False
                    current = await self.client.message(message_id)
                    if (
                        current.get("id") != message_id
                        or current.get("accountId") != self.native_config.account_id
                        or current.get("threadId") != thread
                        or current.get("serverId") != source.get("serverId")
                        or current.get("pat") != source.get("pat")
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

                async def check_current_source():
                    if self.loop_guard.blocked(thread):
                        raise ValueError("自动回复已静默停止")
                    # Downloads/uploads can overlap recalls; recheck before every submission.
                    if self.stopped:
                        raise RuntimeError("WeChat Native 已停止")
                    if source:
                        status = self.state.message_status(message_id)
                        if status in {"needs_review", "failed", "partial", "recalled"}:
                            raise ValueError("回复来源已撤回或需人工检查，未提交发送")
                        latest = await self.client.message(message_id)
                        if self.state.message_status(message_id) in {
                            "needs_review",
                            "failed",
                            "partial",
                            "recalled",
                        }:
                            raise ValueError("回复来源已撤回或需人工检查，未提交发送")
                        if (
                            latest.get("id") != message_id
                            or latest.get("accountId") != self.native_config.account_id
                            or latest.get("threadId") != thread
                            or latest.get("serverId") != source.get("serverId")
                        ):
                            raise ValueError("reply_source_identity_mismatch")
                        if latest.get("recall") is not None:
                            self.state.recall(thread, source.get("serverId"), message_id)
                            if event:
                                event.stop_event()
                            raise ValueError("回复来源已撤回，未提交发送")
                        original_quote = source.get("quote")
                        latest_quote = latest.get("quote")
                        if isinstance(original_quote, dict) and (
                            not isinstance(latest_quote, dict)
                            or latest_quote.get("serverId") != original_quote.get("serverId")
                            or latest_quote.get("state") == "recalled"
                        ):
                            raise ValueError("引用来源已变化或撤回，未提交发送")
                        original_id = (
                            event.get_extra("wechat_native_quote_original_id") if event else None
                        )
                        if original_id:
                            await self._check_quote_original(source, event, original_id)
                    if event and event.get_extra("wechat_native_quote_invalidated"):
                        raise ValueError("引用来源已失效，未提交发送")
                    if records is not None and not self.summary_cache.contains(records):
                        raise ValueError("摘要来源已失效，未提交发送")
                    if quote:
                        target = await self.client.message(quote["messageId"])
                        if (
                            target.get("id") != quote["messageId"]
                            or target.get("accountId") != self.native_config.account_id
                            or target.get("threadId") != thread
                            or target.get("serverId") != quote["serverId"]
                            or target.get("recall") is not None
                            or self.state.message_status(quote["messageId"]) == "recalled"
                        ):
                            raise ValueError("Native 引用来源已失效，未提交发送")

                for index, part in enumerate(parts):
                    if self.loop_guard.blocked(thread):
                        if message_id and self.state.message_status(message_id) == "replied":
                            self.state.mark_message(message_id, "partial")
                        return bool(index)
                    # Resolve each image with bounded memory, before recording an intent.
                    # No later component is attempted after a failure or unknown outcome.
                    if not isinstance(part, str):
                        loader = (
                            load_outgoing_voice if isinstance(part, Record) else load_outgoing_image
                        )
                        data, content_type = await loader(part)
                    key = "astrbot-" + uuid.uuid4().hex
                    self.state.begin_send(key, message_id, thread)

                    def record_voice_progress(chunks):
                        self.state.record_voice_chunks(key, chunks)
                        if any(chunk.get("state") == "accepted" for chunk in chunks):
                            self.loop_guard.outgoing(thread, key, "", trigger=message_id)

                    logger.debug(
                        "WeChat Native 开始发送回复：message=%s receipt_key=%s", message_id, key
                    )
                    try:
                        if index or not isinstance(part, str):
                            await check_current_source()
                        if self.loop_guard.blocked(thread):
                            raise ValueError("自动回复已静默停止")
                        if isinstance(part, str):
                            receipt = await self.client.send_text(
                                thread, part, key, mentions, quote=quote
                            )
                            mentions, quote = None, None
                        elif isinstance(part, Record):
                            receipt = await self.client.send_voice(
                                thread,
                                data,
                                content_type,
                                key,
                                record_job=lambda job_id: self.state.record_send_id(key, job_id),
                                before_send=check_current_source,
                                record_chunks=record_voice_progress,
                            )
                        else:
                            receipt = await self.client.send_image(
                                thread,
                                data,
                                content_type,
                                key,
                                record_job=lambda job_id: self.state.record_send_id(key, job_id),
                                before_send=check_current_source,
                            )
                    except BaseException as error:
                        state = "needs_review"
                        if isinstance(error, SendError) and error.state in {
                            "failed",
                            "cancelled",
                            "partial",
                        }:
                            state = error.state
                        elif isinstance(error, ValueError) or (
                            isinstance(error, NativeAPIError)
                            and 400 <= error.status < 500
                            and error.status not in {408, 429}
                        ):
                            state = "failed"
                        send_id = error.send_id if isinstance(error, SendError) else None
                        self.state.finish_send(key, state, send_id)
                        if message_id:
                            self.state.mark_message(
                                message_id,
                                "partial"
                                if any(
                                    c["state"] == "accepted" for c in self.state.voice_chunks(key)
                                )
                                else "failed"
                                if state == "cancelled"
                                else state,
                            )
                        if not red_packet:
                            logger.error(
                                "WeChat Native 回复未确认；状态 %s，回执键 %s。"
                                "请检查状态库，勿盲目重发",
                                state,
                                key,
                            )
                        raise
                    self.state.finish_send(key, "accepted", receipt["id"])
                    self.loop_guard.outgoing(
                        thread,
                        key,
                        part if isinstance(part, str) else "",
                        trigger=message_id,
                    )
                    if message_id:
                        self.state.mark_message(message_id, "replied")
                    if not red_packet:
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
        tasks = {
            task
            for task in (self._send_tasks | self._media_tasks | self.actions.tasks)
            if task is not current
        }
        if self._run_task and self._run_task is not current:
            tasks.add(self._run_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in list(self._events.values()):
            event.stop_event()
        self.summary_cache.clear()
        self._recent_group_images.clear()
        self._recent_group_pdfs.clear()
        self._recent_group_voices.clear()
        await self.client.close()
        if self.admin_client:
            await self.admin_client.close()
        self.state.close()
        self._state_closed = True
