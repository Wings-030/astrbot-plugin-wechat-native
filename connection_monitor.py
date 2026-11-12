"""Independent read-only health monitoring with durable incident deduplication."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from astrbot.api import logger

from .config import NativeConfig
from .connection_probe import identity_hash, probe_connection, saved_robots


def robot_identity(bot):
    try:
        return identity_hash(NativeConfig.from_dict(bot))
    except (TypeError, ValueError):
        return hashlib.sha256(
            json.dumps([bot.get("server_url"), bot.get("account_id")], sort_keys=True).encode()
        ).hexdigest()


def valid_incident(value, bot_id, identity):
    return (
        isinstance(value, dict)
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("bot_id") == bot_id
        and value.get("identity_hash") == identity
        and value.get("phase") in ("UNKNOWN", "ONLINE", "INCIDENT_OPEN")
        and all(
            type(value.get(k)) is int and 0 <= value[k] <= maximum
            for k, maximum in (("failures", 3), ("recoveries", 2))
        )
        and all(type(value.get(k)) is bool for k in ("outage_attempted", "recovery_attempted"))
        and value.get("last_health") in ("unknown", "online", "offline", "unavailable")
        and value.get("failure_kind")
        in ("", "unauthenticated", "connection_offline", "service_unavailable")
        and (
            value.get("incident_id") is None
            or isinstance(value["incident_id"], str)
            and bool(value["incident_id"])
        )
        and (value["phase"] != "INCIDENT_OPEN" or bool(value.get("incident_id")))
        and all(
            value.get(k) is None or isinstance(value[k], str)
            for k in ("last_outage_attempt", "last_recovery_attempt")
        )
        and all(
            value.get(k)
            in (
                None,
                "attempted",
                "sent",
                "failed",
                "skipped_no_binding",
                "skipped_invalid_platform",
            )
            for k in ("last_outage_result", "last_recovery_result")
        )
    )


class ConnectionMonitor:
    def __init__(self, context, kv, bindings, sender, *, probe=None):
        self.context, self.kv, self.bindings, self.sender = context, kv, bindings, sender
        self.probe = probe or probe_connection
        self._states, self._locks = {}, {}
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="wechat-native-connection-monitor")

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            try:
                await self.check_all()
            except Exception:
                logger.warning("Native 连接监控本轮失败，将在下一周期继续")
            await asyncio.sleep(60)

    async def get_state(self, bot):
        bot_id, identity = bot["id"], robot_identity(bot)
        cached = self._states.get(bot_id)
        if cached and cached["identity_hash"] == identity:
            return dict(cached)
        value = await self.kv.get_kv_data(f"connection_incident:{bot_id}", None)
        if not valid_incident(value, bot_id, identity):
            if value is not None:
                logger.warning("Native 事故记录失效或身份已变更，重新建立基线")
            value = {
                "schema_version": 1,
                "bot_id": bot_id,
                "identity_hash": identity,
                "phase": "UNKNOWN",
                "incident_id": None,
                "failures": 0,
                "recoveries": 0,
                "failure_kind": "",
                "last_health": "unknown",
                "outage_attempted": False,
                "recovery_attempted": False,
                "last_outage_attempt": None,
                "last_recovery_attempt": None,
                "last_outage_result": None,
                "last_recovery_result": None,
            }
        return dict(value)

    def _current(self, bot):
        return any(
            r["id"] == bot["id"] and robot_identity(r) == robot_identity(bot)
            for r in saved_robots(self.context, enabled_only=True)
        )

    async def check_all(self):
        # Independent robots run concurrently; each robot serializes its own probe.
        bots = [dict(bot) for bot in saved_robots(self.context, enabled_only=True)]
        await asyncio.gather(*(self.check_bot(bot) for bot in bots))
        enabled = {b["id"] for b in bots}
        for bot_id in set(self._states) - enabled:
            self._states.pop(bot_id, None)

    async def check_bot(self, bot):
        async with self._locks.setdefault(bot["id"], asyncio.Lock()):
            try:
                if not self._current(bot):
                    return
                state = await self.get_state(bot)
                result = await self.probe(bot)
                if not self._current(bot):
                    return
                state["last_health"] = result.health
                kind = None
                if result.health == "online":
                    state["failures"] = 0
                    if state["phase"] == "INCIDENT_OPEN":
                        state["recoveries"] = min(2, state["recoveries"] + 1)
                        if state["recoveries"] == 2:
                            state["phase"], kind = "ONLINE", "recovery"
                    else:
                        state["phase"], state["recoveries"] = "ONLINE", 0
                else:
                    state["recoveries"] = 0
                    state["failures"] = min(3, state["failures"] + 1)
                    state["failure_kind"] = result.failure_kind
                    if state["phase"] != "INCIDENT_OPEN" and state["failures"] == 3:
                        state.update(
                            phase="INCIDENT_OPEN",
                            incident_id=str(uuid4()),
                            outage_attempted=False,
                            recovery_attempted=False,
                            last_outage_attempt=None,
                            last_recovery_attempt=None,
                            last_outage_result=None,
                            last_recovery_result=None,
                        )
                        kind = "outage"
                binding = None
                if kind:
                    # Consume an event even without a binding; later binding never backfills it.
                    state[kind + "_attempted"] = True
                    state["last_" + kind + "_attempt"] = datetime.now(timezone.utc).isoformat()
                    state["last_" + kind + "_result"] = "attempted"
                    try:
                        binding = await self.bindings.get(bot["id"])
                        if binding is None:
                            state["last_" + kind + "_result"] = "skipped_no_binding"
                    except Exception:
                        state["last_" + kind + "_result"] = "failed"
                # Retain consumed events in memory even if storage fails. A later
                # successful write cannot turn a skipped event into a new send.
                self._states[bot["id"]] = dict(state)
                key = f"connection_incident:{bot['id']}"
                await self.kv.put_kv_data(key, state)
                if kind and binding and self._current(bot):
                    state["last_" + kind + "_result"] = await self.sender.send(
                        binding, bot["id"], kind, state["failure_kind"]
                    )
                    self._states[bot["id"]] = dict(state)
                    await self.kv.put_kv_data(key, state)
            except Exception:
                logger.warning("Native 单机器人监控失败，跳过本次发送")
