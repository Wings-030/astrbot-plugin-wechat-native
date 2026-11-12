"""Shared, read-only Native health classification and saved robot selection."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .config import ADAPTER_ID, NativeConfig
from .native_client import NativeAPIError, NativeClient


def saved_robots(context, *, enabled_only=False):
    return [
        robot
        for robot in context.get_config().get("platform", [])
        if isinstance(robot, dict)
        and robot.get("type") == ADAPTER_ID
        and isinstance(robot.get("id"), str)
        and robot["id"]
        and (not enabled_only or robot.get("enable") is True)
    ]


def identity_hash(config):
    parsed = urlsplit(config.api_url)
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        host += f":{port}"
    canonical = urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", ""))
    return hashlib.sha256(json.dumps([canonical, config.account_id]).encode()).hexdigest()


@dataclass(frozen=True)
class ProbeResult:
    health: str
    reason: str = ""
    auth_state: str = "unknown"
    failure_kind: str = "service_unavailable"

    def public(self):
        return {
            "state": "failed" if self.health == "unavailable" else "connected",
            "wechat_online": None if self.health == "unavailable" else self.health == "online",
            "reason": self.reason,
            "health": self.health,
            "auth_state": self.auth_state,
        }


async def probe_connection(robot):
    try:
        config = NativeConfig.from_dict(robot)
        async with asyncio.timeout(5):
            token = await asyncio.to_thread(config.load_token)
            async with NativeClient(
                config.api_url, config.account_id, token, request_timeout=5
            ) as client:
                status = await client.status()
                if not isinstance(status.get("state"), str):
                    return ProbeResult("unavailable", "Native 服务响应无效")
                if status["state"] != "ready":
                    return ProbeResult("unavailable", "Native 服务尚未就绪")
                account = await client.account()
                if (
                    not isinstance(account.get("id"), str)
                    or type(account.get("authenticated")) is not bool
                    or not isinstance(account.get("connection"), str)
                ):
                    return ProbeResult("unavailable", "Native 账号响应无效")
                if account["id"] != config.account_id:
                    return ProbeResult("unavailable", "Account ID 与 Native 返回的账号不一致")
                if not account["authenticated"]:
                    return ProbeResult(
                        "offline", "微信账号尚未登录", "unauthenticated", "unauthenticated"
                    )
                if account["connection"] != "online":
                    return ProbeResult(
                        "offline", "微信账号当前不在线", "authenticated", "connection_offline"
                    )
                return ProbeResult("online", auth_state="authenticated", failure_kind="")
    except (TypeError, ValueError):
        return ProbeResult("unavailable", "连接配置无效，请检查地址、Account ID 和 Token 配置")
    except TimeoutError:
        return ProbeResult("unavailable", "无法连接 Native 服务或请求超时")
    except NativeAPIError as error:
        reasons = {
            0: "无法连接 Native 服务或请求超时",
            401: "Native Token 无效或已过期",
            403: "Native Token 无权读取服务或该账号",
            404: "Native API 地址或 Account ID 不存在",
            502: "Native 服务响应无效",
        }
        return ProbeResult("unavailable", reasons.get(error.status, "Native 服务暂时不可用"))
