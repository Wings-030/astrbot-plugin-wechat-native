"""AstrBot plugin entry point. Import registers the robot type in the dashboard."""

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .adapter import WeChatNativeAdapter  # noqa: F401
from .config import PLUGIN_ID
from .connection_view import ConnectionView
from .event import WeChatNativeEvent
from .summary import SUMMARY_SYSTEM_PROMPT, summary_prompt


class WeChatNativePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._summary_running = set()
        view = ConnectionView(context)
        context.register_web_api(
            f"/{PLUGIN_ID}/connection/bots", view.bots, ["GET"], "列出微信机器人"
        )
        context.register_web_api(
            f"/{PLUGIN_ID}/connection/check", view.check, ["GET"], "只读测试 Native 连接"
        )

    @filter.command("总结群聊", alias={"群聊摘要"})
    async def summarize_group(self, event: AstrMessageEvent):
        """在 Native 群内真正 @机器人，生成近期群聊摘要。"""
        if not isinstance(event, WeChatNativeEvent) or not event.get_group_id():
            return
        mentions = event.message_obj.raw_message.get("mentions")
        if not (
            isinstance(mentions, dict)
            and mentions.get("state") == "known"
            and mentions.get("self") is True
        ):
            return
        text = await self._generate_group_summary(event)
        if not event.is_stopped():
            yield event.plain_result(text)
            event.stop_event()

    async def _generate_group_summary(self, event: WeChatNativeEvent) -> str:
        adapter = event.adapter
        if not adapter.native_config.enable_group_summary:
            return "请先在 WeChat Native 机器人参数中开启“启用群聊摘要”，开启后开始收集群文本。"
        thread = event.get_group_id()
        key = (adapter, thread)
        if key in self._summary_running:
            return "这个群的摘要正在生成，请稍候再试。"
        records = adapter.summary_cache.snapshot(thread)
        if not records:
            return "暂无可总结的群聊记录。仅收集开启摘要功能后收到的近期群文本，重启后清空。"
        self._summary_running.add(key)
        try:
            try:
                async with asyncio.timeout(60):
                    provider_id = await self.context.get_current_chat_provider_id(
                        event.unified_msg_origin
                    )
                    response = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=summary_prompt(records),
                        system_prompt=SUMMARY_SYSTEM_PROMPT,
                    )
            except TimeoutError:
                return "群聊摘要生成超时，请稍后重试。"
            except Exception:
                self.logger.warning("群聊摘要生成失败，请检查当前会话的模型配置或服务状态")
                return "群聊摘要生成失败，请检查当前会话的模型配置或稍后重试。"
            if not adapter.summary_cache.contains(records):
                return "摘要来源记录已撤回、过期或超出缓存范围，请重试。"
            text = response.completion_text
            if not isinstance(text, str) or not text.strip():
                return "模型返回了空摘要，请稍后重试。"
            event.set_extra("wechat_native_summary_records", records)
            return "群聊摘要（基于当前群的近期缓存）\n" + text.strip()
        finally:
            self._summary_running.discard(key)
