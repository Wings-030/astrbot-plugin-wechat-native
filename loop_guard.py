"""Bounded, account/thread-local automatic reply circuit breaker."""

import asyncio
import hashlib
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from astrbot.api import logger

WINDOW = 60
COOLDOWN = 600
MAX_THREADS = 1024


@dataclass
class Trace:
    events: deque = field(default_factory=lambda: deque(maxlen=64))
    responses: deque = field(default_factory=lambda: deque(maxlen=8))


def fingerprint(text):
    text = " ".join(unicodedata.normalize("NFKC", text[:1024]).casefold().split())[:256]
    if text and len(text) < 3:
        return frozenset([hashlib.sha256(text.encode()).digest()[:8]])
    return (
        frozenset(
            hashlib.sha256(text[i : i + 3].encode()).digest()[:8]
            for i in range(max(0, len(text) - 2))
        )
        if text
        else frozenset()
    )


def similar(left, right, threshold):
    return bool(left and right) and len(left & right) / len(left | right) >= threshold


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
        trace = self.traces.pop(thread, Trace())
        for rows in (trace.events, trace.responses):
            while rows and rows[0][0] <= now - WINDOW:
                rows.popleft()
        self.traces[thread] = trace
        while len(self.traces) > MAX_THREADS:
            self.traces.popitem(last=False)
        return trace

    def trip(self, thread, rule):
        self.state.set_cooldown(self.account, thread, self.clock() + COOLDOWN)
        self.traces.pop(thread, None)
        digest = hashlib.sha256(f"{self.account}:{thread}".encode()).hexdigest()[:12]
        logger.warning("WeChat Native 静默熔断：chat=%s rule=%s window=%s", digest, rule, WINDOW)

    def incoming(self, thread, message_id, text):
        trace = self._trace(thread)
        if not any(row[2] == message_id and row[1] == "in" for row in trace.events):
            trace.events.append((self.clock(), "in", message_id, fingerprint(text), None))

    def outgoing(self, thread, message_id, text, *, trigger=None):
        if self.blocked(thread):
            return
        trace = self._trace(thread)
        if any(row[1] == message_id for row in trace.responses):
            return
        trace.responses.append((self.clock(), message_id))
        trace.events.append((self.clock(), "out", message_id, fingerprint(text), trigger))
        if len(trace.responses) >= 8:
            self.trip(thread, "hard_limit")
            return
        rounds = list(trace.events)[-6:]
        if len(rounds) != 6 or [row[1] for row in rounds] != ["in", "out"] * 3:
            return
        if any(rounds[i + 1][4] != rounds[i][2] for i in (0, 2, 4)):
            return
        fast = all(0 <= rounds[i + 1][0] - rounds[i][0] <= 10 for i in (0, 2, 4))
        threshold = 0.7 if fast else 0.9
        if all(similar(rounds[i][3], rounds[i + 2][3], threshold) for i in range(4)):
            self.trip(thread, "rapid_repeated_rounds" if fast else "repeated_rounds")
