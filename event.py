"""AstrBot message conversion and event lifecycle."""

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, AtAll, Plain, Reply
from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType


def message_timestamp(raw: dict) -> int | None:
    value = raw.get("createdAt")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return int(stamp.timestamp())
    except (ValueError, OverflowError):
        return None


def session_id(account: str, thread: str, sender: str | None = None) -> str:
    group = thread.endswith("@chatroom")
    parts = [quote(account, safe=""), "group" if group else "private", quote(thread, safe="")]
    if group and sender:
        parts.append(quote(sender, safe=""))
    return "|".join(parts)


def session_target(value: str, account: str) -> str:
    parts = value.split("|")
    if (
        len(parts) not in {3, 4}
        or unquote(parts[0]) != account
        or parts[1] not in {"group", "private"}
        or not parts[2]
    ):
        raise ValueError("会话不属于当前 Native 账号或会话格式无效")
    thread = unquote(parts[2])
    if (parts[1] == "group") != thread.endswith("@chatroom"):
        raise ValueError("会话类型与目标不匹配")
    if parts[1] == "private" and len(parts) != 3:
        raise ValueError("无效私聊会话")
    return thread


def convert_message(raw: dict, config, self_id: str) -> AstrBotMessage:
    result = AstrBotMessage()
    thread, sender = raw["threadId"], raw["senderId"]
    group = thread.endswith("@chatroom")
    result.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    result.self_id = self_id
    result.session_id = session_id(
        config.account_id, thread, sender if config.unique_session else None
    )
    result.message_id = raw["id"]
    result.sender = MessageMember(user_id=sender, nickname=sender)
    result.group = Group(group_id=thread) if group else None
    text = raw["body"]
    mentions = raw.get("mentions")
    if (
        group
        and isinstance(mentions, dict)
        and mentions.get("state") == "known"
        and mentions.get("self") is True
    ):
        # WeChat terminates visible mention labels with U+2005. Keep the raw
        # body intact, and never treat an ordinary typed @name as metadata.
        text = re.sub(r"^(?:@[^\u2005\r\n]+\u2005[ \t]*)+", "", text)
    result.message_str = text
    result.message = []
    # In all mode this is an explicit policy wake marker, not evidence of a real @.
    # The unmodified Native mentions remain available in raw_message.
    if group:
        result.message.append(At(qq=self_id))
    quote = raw.get("quote")
    if raw.get("type") == 49 and raw.get("appMessageType") == 57 and isinstance(quote, dict):
        state = quote.get("state")
        quoted_text = "引用消息内容不可用"
        if state == "recalled":
            quoted_text = "引用消息已撤回"
        elif state == "unsupported":
            quoted_text = "不支持此引用消息的内容"
        elif state == "embedded" and isinstance(quote.get("embeddedText"), str):
            quoted_text = quote["embeddedText"][:16384] or quoted_text
        result.message.append(
            Reply(
                id=quote.get("serverId") if isinstance(quote.get("serverId"), str) else "",
                sender_id=quote.get("senderId") if isinstance(quote.get("senderId"), str) else "",
                message_str=quoted_text,
                chain=[Plain(quoted_text)],
            )
        )
    result.message.append(Plain(text))
    result.raw_message = raw
    result.timestamp = message_timestamp(raw) or int(datetime.now(UTC).timestamp())
    return result


def serialize_text(chain: MessageChain, *, group: bool) -> tuple[str, dict | None, str | None]:
    text, users, all_members = [], [], False
    reply_id = None
    for component in chain.chain:
        if isinstance(component, Plain):
            text.append(component.text)
        elif isinstance(component, At):
            user = str(component.qq)
            if user == "all":
                all_members = True
            elif user not in users:
                users.append(user)
        elif isinstance(component, AtAll):
            all_members = True
        elif isinstance(component, Reply):
            target = str(component.id).strip()
            if not target or (reply_id is not None and reply_id != target):
                raise ValueError("Native 引用回复需要且只能指定一条原消息")
            reply_id = target
        else:
            raise ValueError("WeChat Native 首版仅支持文本和群 @，请关闭文本转图片")
    if (users or all_members) and not group:
        users, all_members = [], False
    if users and all_members:
        raise ValueError("不能同时 @个人和 @全体")
    mentions = {"all": True} if all_members else {"userIds": users} if users else None
    return "".join(text), mentions, reply_id


class WeChatNativeEvent(AstrMessageEvent):
    def __init__(self, message, adapter):
        super().__init__(message.message_str, message, adapter.meta(), message.session_id)
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        if self.adapter.stopped:
            raise RuntimeError("WeChat Native 已停止")
        status = self.adapter.state.message_status(self.message_obj.message_id)
        if status == "recalled":
            self.stop_event()
            return
        if status in {"needs_review", "failed", "partial"}:
            raise RuntimeError("此消息的回复需人工检查，禁止自动重发")
        try:
            sent = await self.adapter.send_chain(
                self.message_obj.raw_message["threadId"],
                message,
                source=self.message_obj.raw_message,
                event=self,
            )
        except (Exception, asyncio.CancelledError) as error:
            # Includes validation/readback failures before an outbox entry exists.
            if not self.adapter._state_closed:
                current = self.adapter.state.message_status(self.message_obj.message_id)
                if current not in {"failed", "needs_review", "partial", "recalled"}:
                    self.adapter.state.mark_message(
                        self.message_obj.message_id,
                        "failed" if isinstance(error, ValueError) else "needs_review",
                    )
            raise
        if sent:
            await super().send(message)
        elif self.adapter.state.message_status(self.message_obj.message_id) == "queued":
            self.adapter.state.mark_message(self.message_obj.message_id, "no_reply")

    async def send_streaming(self, generator, use_fallback=False) -> None:
        # Native has no token streaming. Concatenate delta chains and send once.
        chain = MessageChain()
        async for part in generator:
            chain.chain.extend(part.chain)
        await self.send(chain)
