"""Offline notifications for the WeChat Native account.

The adapter already reconnects on its own; this module only watches the account
state and reports outages through an already-configured AstrBot platform
(``weixin_oc`` by default), so nobody has to poll the dashboard.
"""

import asyncio
import json
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, StarTools

from .config import PLUGIN_ID
from .login_qr import create_login_qr
from .native_client import NativeAPIError

POLL_SECONDS = 30
NOTIFY_TYPE = "weixin_oc"
STATE_FILE = "offline_notify_state.json"


@dataclass
class _Runtime:
    last_check: float = float("-inf")
    fail_streak: int = 0
    online: bool | None = None
    down_since: float | None = None
    last_alert_ts: float = 0.0
    qr_sent: bool = False


def _duration_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} 小时 {rest} 分钟"
    return f"{hours} 小时"


def _time_text(timestamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp))


class OfflineNotifyManager:
    def __init__(self, context: Context, logger, state_dir: Path | None = None):
        self.context = context
        self.logger = logger
        root = state_dir if state_dir is not None else StarTools.get_data_dir(PLUGIN_ID)
        self.state_path = Path(root) / STATE_FILE
        self._state = self._load_state()
        self._runtime: dict[str, _Runtime] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        # Platforms are created after plugin initialization; give the loader
        # a moment before the first probe.
        await asyncio.sleep(5)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning("掉线提醒巡检异常", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    async def poll_once(self) -> None:
        for adapter in self._adapters():
            config = getattr(adapter, "native_config", None)
            if config is None or not (config.enable_offline_notify or config.enable_email_notify):
                continue
            platform_id = adapter.meta().id
            runtime = self._runtime.get(platform_id)
            if runtime is None:
                runtime = self._runtime[platform_id] = self._runtime_from_state(platform_id)
            now = time.monotonic()
            if now - runtime.last_check < POLL_SECONDS:
                continue
            runtime.last_check = now
            online, reason, needs_login = await self._probe(adapter)
            await self._apply(adapter, runtime, online, reason, needs_login)
            self._save_state()

    def _adapters(self) -> list:
        manager = getattr(self.context, "platform_manager", None)
        instances = getattr(manager, "platform_insts", None) or []
        result = []
        for instance in instances:
            try:
                meta = instance.meta()
            except Exception:
                continue
            if meta.name != "wechat_native" or getattr(instance, "stopped", False):
                continue
            result.append(instance)
        return result

    async def _probe(self, adapter) -> tuple[bool, str, bool]:
        client = getattr(adapter, "client", None)
        if client is None:
            return False, "Native 客户端未初始化", False
        try:
            account = await client.account()
        except NativeAPIError as error:
            return False, f"Native 接口错误（HTTP {getattr(error, 'status', '?')}）", False
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return False, f"Native 接口不可达（{type(error).__name__}）", False
        if account.get("authenticated") and account.get("connection") == "online":
            return True, "online", False
        return (
            False,
            f"authenticated={account.get('authenticated')} "
            f"connection={account.get('connection')}",
            not account.get("authenticated"),
        )

    async def _apply(
        self,
        adapter,
        runtime: _Runtime,
        online: bool,
        reason: str,
        needs_login: bool = False,
    ) -> None:
        config = adapter.native_config
        now = time.time()
        if online:
            runtime.fail_streak = 0
            if runtime.online is False:
                downtime = now - runtime.down_since if runtime.down_since else 0.0
                if config.offline_notify_on_recovery:
                    await self._send(
                        adapter,
                        f"微信个人号已恢复在线 {_time_text(now)}",
                        "✅ 微信个人号已恢复在线\n"
                        f"时间：{_time_text(now)}\n"
                        f"掉线时长：约 {_duration_text(downtime)}",
                    )
                runtime.down_since = None
                runtime.last_alert_ts = 0.0
            runtime.online = True
            runtime.qr_sent = False
            return

        runtime.fail_streak += 1
        down_for = runtime.fail_streak * POLL_SECONDS
        if down_for < config.offline_notify_after_seconds:
            return
        went_offline = runtime.online is not False
        if went_offline:
            runtime.online = False
            runtime.down_since = now - down_for
            runtime.last_alert_ts = 0.0
            runtime.qr_sent = False
        if runtime.last_alert_ts:
            repeat_hours = config.offline_notify_repeat_hours
            if repeat_hours == 0 or now - runtime.last_alert_ts < repeat_hours * 3600:
                return
        if await self._send(
            adapter,
            f"微信个人号掉线了 {_time_text(now)}",
            "⚠️ 微信个人号掉线了\n"
            f"时间：{_time_text(now)}\n"
            f"持续：{_duration_text(down_for)}\n"
            f"状态：{reason}\n"
            "处理：在服务器重新扫码登录",
        ):
            runtime.last_alert_ts = now
        if needs_login and went_offline and not runtime.qr_sent:
            runtime.qr_sent = await self._send_login_qr(adapter)

    async def _send_login_qr(self, adapter) -> bool:
        config = adapter.native_config
        if not config.enable_login_qr:
            return False
        umo = self._resolve_umo(adapter)
        if not umo:
            self.logger.warning("登录二维码未发送：未找到 weixin_oc 会话")
            return False
        try:
            path = await create_login_qr(adapter.client, self.state_path.parent)
        except Exception as error:
            self.logger.warning("登录二维码生成失败：%s", type(error).__name__)
            return False
        try:
            chain = MessageChain(
                [
                    Plain(
                        "⚠️ 微信个人号掉线，请用手机微信扫码登录。"
                        "二维码约 4 分钟内有效，过期后发送「微信二维码」重新获取。"
                    ),
                    Image.fromFileSystem(str(path)),
                ]
            )
            found = await self.context.send_message(umo, chain)
        except Exception as error:
            self.logger.warning("登录二维码发送失败：%s", type(error).__name__)
            return False
        if not found:
            self.logger.warning("登录二维码发送失败：目标平台不可用")
            return False
        self.logger.info("登录二维码已发送")
        return True

    async def _send(self, adapter, subject: str, text: str) -> bool:
        config = adapter.native_config
        delivered = False
        if config.enable_offline_notify:
            delivered = await self._send_umo(adapter, text) or delivered
        if config.enable_email_notify:
            delivered = await self._send_email(config, subject, text) or delivered
        if not delivered:
            self.logger.warning("掉线提醒没有送达任何通道")
        return delivered

    async def _send_email(self, config, subject: str, text: str) -> bool:
        if not config.email_smtp_host or not config.email_to:
            self.logger.warning("邮件提醒未发送：缺少 SMTP 服务器地址或收件人")
            return False
        try:
            await asyncio.to_thread(self._send_email_sync, config, subject, text)
        except Exception as error:
            self.logger.warning("邮件提醒发送失败：%s", type(error).__name__)
            return False
        self.logger.info("邮件提醒已发送")
        return True

    @staticmethod
    def _send_email_sync(config, subject: str, text: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.email_from or config.email_smtp_user or "wechat-native@localhost"
        message["To"] = config.email_to
        message.set_content(text)
        timeout = 15
        if config.email_smtp_security == "ssl":
            with smtplib.SMTP_SSL(
                config.email_smtp_host, config.email_smtp_port, timeout=timeout
            ) as server:
                if config.email_smtp_user:
                    server.login(config.email_smtp_user, config.email_smtp_password)
                server.send_message(message)
            return
        with smtplib.SMTP(
            config.email_smtp_host, config.email_smtp_port, timeout=timeout
        ) as server:
            if config.email_smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if config.email_smtp_user:
                server.login(config.email_smtp_user, config.email_smtp_password)
            server.send_message(message)

    async def _send_umo(self, adapter, text: str) -> bool:
        umo = self._resolve_umo(adapter)
        if not umo:
            self.logger.warning(
                "掉线提醒未发送：未找到 weixin_oc 会话，请先给官方 AI 机器人发一条消息"
            )
            return False
        try:
            found = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as error:
            self.logger.warning("掉线提醒发送失败：%s", type(error).__name__)
            self._forget_learned_umo()
            return False
        if not found:
            self.logger.warning("掉线提醒发送失败：目标平台 %s 不可用", umo.split(":", 1)[0])
            self._forget_learned_umo()
            return False
        self.logger.info("掉线提醒已发送到 %s", umo.split(":", 1)[0])
        return True

    def _resolve_umo(self, adapter) -> str | None:
        explicit = adapter.native_config.offline_notify_umo
        if explicit:
            return explicit
        learned = self._state.get("umo")
        if isinstance(learned, str) and learned:
            return learned
        best = self._best_wxoc_umo()
        if best:
            self._state["umo"] = best
            self._save_state()
        return best

    def _forget_learned_umo(self) -> None:
        if self._state.pop("umo", None) is not None:
            self._save_state()

    def _best_wxoc_umo(self) -> str | None:
        manager = getattr(self.context, "platform_manager", None)
        instances = getattr(manager, "platform_insts", None) or []
        candidates: list[tuple[float, str]] = []
        for instance in instances:
            try:
                meta = instance.meta()
            except Exception:
                continue
            if meta.name != NOTIFY_TYPE:
                continue
            tokens = getattr(instance, "_context_tokens", None)
            if not isinstance(tokens, dict) or not tokens:
                continue
            recent = getattr(instance, "_recent_messages", None)
            if not isinstance(recent, dict):
                recent = {}
            for user_id in tokens:
                entry = recent.get(user_id)
                score = float(getattr(entry, "updated_at", 0.0) or 0.0) if entry else 0.0
                candidates.append((score, f"{meta.id}:FriendMessage:{user_id}"))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            # Without a clear "most recent" session, guessing could notify the
            # wrong person; wait until the owner messages the bot again.
            return None
        return candidates[0][1]

    def _runtime_from_state(self, platform_id: str) -> _Runtime:
        raw = (self._state.get("platforms") or {}).get(platform_id) or {}
        online = raw.get("online")
        down_since = raw.get("down_since")
        last_alert = raw.get("last_alert_ts")
        return _Runtime(
            online=online if isinstance(online, bool) else None,
            down_since=float(down_since) if isinstance(down_since, (int, float)) else None,
            last_alert_ts=float(last_alert) if isinstance(last_alert, (int, float)) else 0.0,
        )

    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_state(self) -> None:
        platforms = {
            platform_id: {
                "online": runtime.online,
                "down_since": runtime.down_since if runtime.online is False else None,
                "last_alert_ts": runtime.last_alert_ts,
            }
            for platform_id, runtime in self._runtime.items()
        }
        state = {"version": 1, "umo": self._state.get("umo", ""), "platforms": platforms}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.state_path)
        except OSError:
            self.logger.warning("掉线提醒状态写入失败", exc_info=True)
