"""Administrator private-chat alert bindings in AstrBot Plugin KV."""

import asyncio
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .alert_sender import AlertSender, enabled_platform
from .connection_probe import saved_robots


class BindingError(Exception):
    """A fixed, public command rejection, never a dependency exception."""


def valid_binding(value):
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or any(
            not isinstance(value.get(k), str) or not value[k]
            for k in ("platform_type", "platform_id", "umo", "bound_at")
        )
    ):
        return False
    try:
        session = MessageSession.from_str(value["umo"])
        datetime.fromisoformat(value["bound_at"])
        return (
            session.message_type == MessageType.FRIEND_MESSAGE
            and session.platform_id == value["platform_id"]
            and bool(session.session_id)
        )
    except (ValueError, TypeError):
        return False


class AlertBindingService:
    def __init__(self, context, kv):
        self.context, self.kv = context, kv
        self.sender = AlertSender(context)
        self.monitor = None
        self._locks = {}

    async def get(self, bot_id):
        value = await self.kv.get_kv_data(f"connection_alert_binding:{bot_id}", None)
        if value is None:
            return None
        if not valid_binding(value):
            logger.warning("Native 通知绑定记录无效，已跳过")
            return None
        return value

    def _target(self, bot_id):
        bots = saved_robots(self.context, enabled_only=True)
        if bot_id:
            for bot in bots:
                if bot["id"] == bot_id:
                    return bot
            raise BindingError("请选择已启用的 WeChat Native 机器人 ID")
        if len(bots) == 1:
            return bots[0]
        if not bots:
            raise BindingError("没有已启用的 WeChat Native 机器人")
        raise BindingError("请指定 Native 机器人 ID：" + "、".join(bot["id"] for bot in bots))

    def _trusted_binding(self, event):
        if (
            not isinstance(event, AstrMessageEvent)
            or event.is_admin() is not True
            or event.is_private_chat() is not True
        ):
            raise BindingError("此指令仅允许已认证的管理员私聊操作")
        value = {
            "schema_version": 1,
            "platform_type": event.get_platform_name(),
            "platform_id": event.get_platform_id(),
            "umo": event.unified_msg_origin,
            "bound_at": datetime.now(timezone.utc).isoformat(),
        }
        if not valid_binding(value) or not enabled_platform(
            self.context, value["platform_id"], value["platform_type"]
        ):
            raise BindingError("当前私聊的平台身份无效或平台未启用")
        return value

    async def execute(self, action, event, bot_id=""):
        try:
            incoming = self._trusted_binding(event)
            if not isinstance(bot_id, str) or any(c.isspace() for c in bot_id):
                raise BindingError("只接受一个 Native 机器人 ID 参数")
            bot = self._target(bot_id)
            bot_id = bot["id"]
            async with self._locks.setdefault(bot_id, asyncio.Lock()):
                key = f"connection_alert_binding:{bot_id}"
                if action == "bind":
                    old = await self.get(bot_id)
                    if old and all(
                        old[k] == incoming[k] for k in ("platform_type", "platform_id", "umo")
                    ):
                        incoming = old
                    await self.kv.put_kv_data(key, incoming)
                    return (
                        f"✅ 已绑定 {bot_id}，通知将发送到当前 {incoming['platform_type']} 私聊。"
                    )
                if action == "unbind":
                    await self.kv.delete_kv_data(key)
                    return f"✅ 已解绑 {bot_id} 的通知目标。"
                binding = await self.get(bot_id)
                if action == "test":
                    if not binding:
                        return "尚未绑定通知，请先执行 /绑定通知。"
                    result = await self.sender.send(binding, bot_id, "test")
                    return (
                        "通知发送调用已完成（不保证最终送达）。"
                        if result == "sent"
                        else "通知尝试失败，请检查目标平台与主动消息权限；不会自动重试。"
                    )
                if action == "status":
                    state = await self.monitor.get_state(bot) if self.monitor else {}
                    health = {
                        "online": "Online",
                        "offline": "Offline",
                        "unavailable": "Unavailable",
                    }.get(state.get("last_health"), "尚未检测")
                    platform = binding["platform_type"] if binding else "未绑定"
                    results = {
                        "sent": "发送调用已完成（不保证送达）",
                        "failed": "尝试失败",
                        "skipped_no_binding": "无绑定已跳过",
                        "skipped_invalid_platform": "目标平台不可用已跳过",
                        "attempted": "已记录尝试（结果未知）",
                    }
                    latest = state.get("last_recovery_result") or state.get("last_outage_result")
                    return (
                        f"机器人：{bot_id}；Native: {health}；通知渠道：{platform}；"
                        f"最近通知：{results.get(latest, '尚未尝试')}"
                    )
                raise BindingError("未知通知指令")
        except BindingError as error:
            return str(error)
        except Exception:
            logger.warning("Native 通知指令处理失败，未泄露会话定位信息")
            return "通知操作失败，请检查持久化存储或平台状态后重试。"
