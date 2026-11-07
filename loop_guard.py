"""Bounded, account/thread-local automatic reply circuit breaker."""

import asyncio
import hashlib
import time
from collections import OrderedDict, deque

from astrbot.api import logger

WINDOW = 60
COOLDOWN = 600
MAX_THREADS = 1024


class LoopGuard:
    def __init__(self, state, account, *, clock=time.time):
        self.state, self.account, self.clock = state, account, clock
        self.traces = OrderedDict()
        # Fixed stripes bound lock storage even under a flood of new Thread IDs.
        self.locks = [asyncio.Lock() for _ in range(256)]

    def gate(self, thread):
        digest = hashlib.sha256(thread.encode()).digest()
        return self.locks[int.from_bytes(digest[:2]) % len(self.locks)]

    def blocked(self, thread):
        return self.state.cooldown(self.account, thread) > self.clock()

    def _trace(self, thread):
        now = self.clock()
        trace = self.traces.pop(thread, deque(maxlen=64))
        while trace and trace[0][0] <= now - WINDOW:
            trace.popleft()
        self.traces[thread] = trace
        while len(self.traces) > MAX_THREADS:
            self.traces.popitem(last=False)
        return trace

    def trip(self, thread, rule):
        self.state.set_cooldown(self.account, thread, self.clock() + COOLDOWN)
        self.traces.pop(thread, None)
        digest = hashlib.sha256(f"{self.account}:{thread}".encode()).hexdigest()[:12]
        logger.warning("WeChat Native 静默熔断：chat=%s rule=%s window=%s", digest, rule, WINDOW)

    def outgoing(self, thread, message_id, text):
        if self.blocked(thread):
            return
        trace = self._trace(thread)
        if any(row[1] == message_id for row in trace):
            return
        trace.append((self.clock(), message_id))
        if len(trace) >= 8:
            self.trip(thread, "hard_limit")
