"""Read-only connection checks and statistics for the AstrBot Plugin View."""

import asyncio

from astrbot.api.star import StarTools
from astrbot.api.web import json_response, request

from .config import PLUGIN_ID, NativeConfig
from .connection_probe import probe_connection, saved_robots
from .login_session import LoginError
from .state import read_statistics, state_path


def _json(data):
    return json_response(data, headers={"Cache-Control": "no-store"})


def _result(reason="", online=None):
    return _json(
        {
            "state": "failed" if online is None else "connected",
            "wechat_online": online,
            "reason": reason,
        }
    )


class ConnectionView:
    def __init__(self, context, login_sessions=None):
        self.context = context
        self.login_sessions = login_sessions

    def _robots(self):
        # Read saved settings, including disabled/unstartable robots. Never use
        # the adapter constructor here: it opens the persistent message journal.
        return saved_robots(self.context)

    async def bots(self):
        return _json({"bots": [{"id": robot["id"]} for robot in self._robots()]})

    async def panel(self):
        bot_id = request.query.get("bot_id", "")
        robot = next((robot for robot in self._robots() if robot["id"] == bot_id), None)
        if robot is None:
            return _json({"reason": "请选择已保存的 WeChat Native 机器人"})
        try:
            config = NativeConfig.from_dict(robot)
        except (TypeError, ValueError):
            return _json({"reason": "配置无效，请检查机器人配置"})
        path = state_path(
            StarTools.get_data_dir(PLUGIN_ID), config.api_url, config.account_id, bot_id
        )
        statistics = await asyncio.to_thread(read_statistics, path)
        return _json(
            {
                "statistics": statistics,
                "config": {
                    **{
                        key: getattr(config, key)
                        for key in (
                            "enable_private_reply",
                            "group_reply_mode",
                            "enable_media",
                            "enable_group_summary",
                            "enable_history_tools",
                            "enable_wechat_actions",
                            "enable_group_red_packet_reply",
                            "unique_session",
                        )
                    },
                    "allowed_users": len(config.allowed_users),
                    "allowed_groups": len(config.allowed_groups),
                },
            }
        )

    async def check(self):
        bot_id = request.query.get("bot_id", "")
        robot = next((robot for robot in self._robots() if robot["id"] == bot_id), None)
        if robot is None:
            return _result("请选择已保存的 WeChat Native 机器人")
        return _json((await probe_connection(robot)).public())

    def _authorize_login(self):
        # The pinned AstrBot AuthService authenticates one configured Dashboard
        # account (no separate role API). PluginRequest.username is supplied by
        # its authenticated plugin router, never by a browser parameter.
        username = request.username
        if not username:
            raise LoginError("请先登录 AstrBot Dashboard", 401)
        dashboard = self.context.get_config().get("dashboard")
        admin = dashboard.get("username") if isinstance(dashboard, dict) else None
        if not isinstance(admin, str) or not admin.strip() or username != admin:
            raise LoginError("需要已验证的 Dashboard 管理员权限", 403)

    async def _login(self, action):
        try:
            self._authorize_login()
            values = await request.json() if action == "start" else dict(request.query.items())
            allowed = {"bot_id", "retry"} if action == "start" else {"bot_id", "generation"}
            if (not isinstance(values, dict) or set(values) - allowed
                    or not isinstance(values.get("bot_id"), str)
                    or not values["bot_id"]):
                raise LoginError("登录请求参数无效", 400)
            if any(len(request.query.getlist(key)) != 1 for key in request.query.keys()):
                raise LoginError("登录请求参数重复", 400)
            bot = next((r for r in saved_robots(self.context, enabled_only=True)
                        if r["id"] == values["bot_id"]), None)
            if bot is None:
                raise LoginError("请选择已启用的 WeChat Native 机器人", 404)
            if action == "start":
                retry = values.get("retry", False)
                if type(retry) is not bool:
                    raise LoginError("登录请求参数无效", 400)
                result = await self.login_sessions.start(bot, retry=retry)
            elif action == "status":
                if "generation" in values:
                    raise LoginError("登录请求参数无效", 400)
                result = await self.login_sessions.status(bot)
            else:
                value = values.get("generation", "")
                if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                    raise LoginError("二维码代次无效", 400)
                generation = int(value) if len(value) <= 10 else 0
                if generation < 1:
                    raise LoginError("二维码代次无效", 400)
                result = await self.login_sessions.qr(bot, generation)
            return _json(result)
        except LoginError as error:
            return json_response({"reason": error.reason}, status_code=error.status,
                                 headers={"Cache-Control": "no-store"})

    async def login_start(self):
        return await self._login("start")

    async def login_status(self):
        return await self._login("status")

    async def login_qr(self):
        return await self._login("qr")
