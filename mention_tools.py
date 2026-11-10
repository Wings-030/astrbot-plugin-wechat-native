"""Model-requested mentions through the existing current-group send pipeline."""

import asyncio
import unicodedata

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Plain

from .native_client import SendError
from .tool_support import ToolError, context, encode, error_result, result

RESULT_KEY = "wechat_native_mention_result"


async def run_mention(event, username, text):
    action, adapter, task = "mention_member", None, asyncio.current_task()
    try:
        ctx = context(event, group=True, send=True)
        if (
            not isinstance(username, str)
            or not 1 <= len(username) <= 512
            or username == "all"
            or username.endswith("@chatroom")
            or any(c.isspace() or unicodedata.category(c) == "Cc" for c in username)
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > 65536
        ):
            raise ToolError("invalid_request")
        adapter = ctx.adapter
        adapter._send_tasks.add(task)
        async with adapter.loop_guard.gate(ctx.thread):
            ctx = context(event, group=True, send=True)
            previous = event.get_extra(RESULT_KEY)
            if previous:
                return encode(dict(previous, replayed=True))
            chain = MessageChain([At(qq=username), Plain(text)])
            try:
                # The thread gate is already held; _send_chain owns the send lock.
                sent = await adapter._send_chain(
                    ctx.thread, chain, source=event.message_obj.raw_message, event=event
                )
            except SendError:
                raise ToolError("needs_review") from None
            if not sent:
                raise ToolError("not_available")
            value = result(
                action,
                "success",
                username=username,
                message="Native 已接受当前群的 @消息，无需再次发送相同正文",
            )
            event.set_extra(RESULT_KEY, value)
            await AstrMessageEvent.send(event, chain)
            return encode(value)
    except Exception as error:
        return encode(error_result(action, error))
    finally:
        if adapter:
            adapter._send_tasks.discard(task)
