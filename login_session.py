"""Durable coordination of Native v2.3.0 login attempts; QR bytes stay in memory."""

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from astrbot.api import logger

from .config import NativeConfig
from .connection_probe import identity_hash, probe_connection
from .native_client import NativeAPIError, NativeClient

TERMINAL_STATES = {
    "succeeded",
    "expired",
    "failed",
    "rejected",
    "cancelled",
    "needs_login",
    "needs_review",
}
ACTIVE_STATES = {
    "pending",
    "starting",
    "resuming",
    "authenticating",
    "queued",
    "running",
    "awaiting_scan",
    "awaiting_approval",
}


class LoginError(Exception):
    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.reason, self.status = reason, status


class LoginSessionManager:
    def __init__(self, kv):
        self.kv = kv
        self._locks = {}

    def _lock(self, bot):
        return self._locks.setdefault(bot["id"], asyncio.Lock())

    def _config_key(self, bot):
        try:
            config = NativeConfig.from_dict(bot)
        except (TypeError, ValueError):
            raise LoginError("连接配置无效，请检查机器人配置", 400) from None
        identity = identity_hash(config)
        return config, f"connection_login:{bot['id']}:{identity}", identity

    async def _read(self, key, identity):
        value = await self.kv.get_kv_data(key, None)
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or value.get("identity_hash") != identity
            or not isinstance(value.get("idempotency_key"), str)
            or not value["idempotency_key"]
            or not isinstance(value.get("created_at"), str)
            or value.get("state") not in ACTIVE_STATES | TERMINAL_STATES
            or (
                value.get("operation_id") is not None
                and (not isinstance(value["operation_id"], str) or not value["operation_id"])
            )
        ):
            logger.warning("Native 登录记录无效，已拒绝恢复")
            raise LoginError("登录记录无效，请管理员检查持久化存储", 503)
        return value

    async def _client(self, config):
        try:
            if not config.admin_token_file:
                raise ValueError
            token = await asyncio.to_thread(
                replace(config, token="", token_file=config.admin_token_file).load_token
            )
        except (TypeError, ValueError):
            raise LoginError("Admin Token 文件不可读或无效，请检查挂载与权限", 403) from None
        return NativeClient(config.api_url, config.account_id, token, request_timeout=5)

    @staticmethod
    def _public(value):
        return {"state": value["state"], "qr_available": False}

    async def _fetch(self, client, key, record):
        value = await client.login_status(record["operation_id"])
        record = {**record, "state": value["state"]}
        await self.kv.put_kv_data(key, record)
        result = value["result"]
        available = value["state"] == "awaiting_scan" and result.get("qrAvailable") is True
        public = {"state": value["state"], "qr_available": available}
        if available:
            public["qr_generation"] = result["qrGeneration"]
        return record, public

    @staticmethod
    def _native_error(error):
        if error.status in (401, 403):
            return LoginError("Admin Token 无权登录该账号，请检查授权", 403)
        if error.status == 409:
            return LoginError("登录任务冲突或二维码已更新，请刷新原任务状态", 409)
        return LoginError("Native 登录请求失败，请检查服务后重试", 502)

    async def start(self, bot, *, retry=False):
        async with self._lock(bot):
            try:
                config, key, identity = self._config_key(bot)
                record = await self._read(key, identity)
                health = await probe_connection(bot)
                if health.auth_state != "unauthenticated" or health.health != "offline":
                    raise LoginError("当前账号无需扫码，或 Native 服务不可用", 409)
                async with await self._client(config) as client:
                    if record and record["operation_id"]:
                        if record["state"] in TERMINAL_STATES:
                            public = self._public(record)
                        else:
                            record, public = await self._fetch(client, key, record)
                        if record["state"] not in TERMINAL_STATES or not retry:
                            return public
                    if record is None or record["state"] in TERMINAL_STATES:
                        record = {
                            "schema_version": 1,
                            "identity_hash": identity,
                            "idempotency_key": str(uuid4()),
                            "operation_id": None,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "state": "pending",
                        }
                    # Persist even on retries: a failed earlier write must never reach Native.
                    await self.kv.put_kv_data(key, record)
                    value = await client.start_login(record["idempotency_key"])
                    record = {**record, "operation_id": value["id"], "state": value["state"]}
                    await self.kv.put_kv_data(key, record)
                    result = value["result"]
                    available = (
                        value["state"] == "awaiting_scan" and result.get("qrAvailable") is True
                    )
                    public = {"state": value["state"], "qr_available": available}
                    if available:
                        public["qr_generation"] = result["qrGeneration"]
                    return public
            except LoginError:
                raise
            except NativeAPIError as error:
                raise self._native_error(error) from None
            except Exception:
                logger.warning("Native 登录持久化不可用，已停止本次操作")
                raise LoginError("登录存储不可用，请稍后重试", 503) from None

    async def status(self, bot):
        async with self._lock(bot):
            try:
                config, key, identity = self._config_key(bot)
                record = await self._read(key, identity)
                if not record:
                    return {"state": "idle", "qr_available": False}
                if not record["operation_id"] or record["state"] in TERMINAL_STATES:
                    return self._public(record)
                async with await self._client(config) as client:
                    _, public = await self._fetch(client, key, record)
                    return public
            except LoginError:
                raise
            except NativeAPIError as error:
                raise self._native_error(error) from None
            except Exception:
                logger.warning("Native 登录状态读取失败")
                raise LoginError("登录存储不可用，请稍后重试", 503) from None

    async def qr(self, bot, generation):
        async with self._lock(bot):
            try:
                config, key, identity = self._config_key(bot)
                record = await self._read(key, identity)
                if not record or not record["operation_id"]:
                    raise LoginError("请先准备登录任务")
                async with await self._client(config) as client:
                    _, public = await self._fetch(client, key, record)
                    if (
                        not public["qr_available"]
                        or type(generation) is not int
                        or generation != public.get("qr_generation")
                    ):
                        raise LoginError("二维码已更新或失效，请刷新登录状态")
                    image = await client.login_qr(record["operation_id"], generation)
                return {
                    "image": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                    "generation": generation,
                }
            except LoginError:
                raise
            except NativeAPIError as error:
                raise self._native_error(error) from None
            except Exception:
                logger.warning("Native 二维码读取失败")
                raise LoginError("二维码读取失败，请稍后重试", 503) from None
