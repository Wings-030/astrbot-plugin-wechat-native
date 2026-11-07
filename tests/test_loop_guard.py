import asyncio
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from conftest import module
from test_adapter import (
    make_adapter,
    native_event,
    no_telemetry,  # noqa: F401
)


async def test_hard_limit_silences_pending_replies_and_persists(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        # Different response contents must still hit the hard cap.
        results = await asyncio.gather(
            *[adapter.send_chain("peer", MessageChain([Plain(str(i))])) for i in range(12)]
        )
        assert sum(results) == 8
        assert len(sent) == 8
        await adapter.process_native_event(native_event(msg))
        assert queue.empty()
        assert adapter.state.cursor == "1"
        assert adapter.state.message_status(msg["id"]) == "ignored"
        assert await adapter.send_chain("other", MessageChain([Plain("ok")]))
        state_path = adapter.state.db.execute("PRAGMA database_list").fetchone()[2]
        await adapter.terminate()
        state = module("state").StateStore(Path(state_path))
        guard = module("loop_guard").LoopGuard(state, "a")
        assert guard.blocked("peer")
        assert not guard.blocked("other")
        state.close()
    finally:
        await adapter.terminate()


async def test_cooldown_expiry_and_replay_do_not_count(tmp_path):
    clock = [1000.0]
    state = module("state").StateStore(tmp_path / "state.sqlite3")
    guard = module("loop_guard").LoopGuard(state, "a", clock=lambda: clock[0])
    try:
        for i in range(8):
            guard.outgoing("peer", str(i), "reply")
            guard.outgoing("peer", str(i), "reply")
        assert guard.blocked("peer")
        clock[0] += 601
        assert not guard.blocked("peer")
        assert not module("loop_guard").LoopGuard(state, "b").blocked("peer")
    finally:
        state.close()


async def test_queued_event_cannot_reply_after_trip(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        await adapter.process_native_event(native_event(msg))
        event = queue.get_nowait()
        for i in range(8):
            await adapter.send_chain("peer", MessageChain([Plain(str(i))]))
        await event.send(MessageChain([Plain("late generated answer")]))
        assert len(sent) == 8
        assert event.is_stopped()
        assert adapter.state.message_status(msg["id"]) == "no_reply"
        # Recall events still advance their cursor during cooldown.
        await adapter.process_native_event(
            {
                "cursor": "2",
                "kind": "message.recalled",
                "data": {"threadId": "peer", "messageId": msg["id"], "serverId": "900"},
            }
        )
        assert adapter.state.cursor == "2"
        assert adapter.state.message_status(msg["id"]) == "recalled"
    finally:
        await adapter.terminate()


async def test_repeating_round_trips_trip_before_hard_cap(tmp_path):
    state = module("state").StateStore(tmp_path / "loop.sqlite3")
    guard = module("loop_guard").LoopGuard(state, "a", clock=lambda: 1000)
    try:
        for i in range(3):
            guard.incoming("peer", str(i), "  Ｈｅｌｌｏ   世界 ")
            guard.outgoing("peer", f"out-{i}", "你好 世界", trigger=str(i))
        assert guard.blocked("peer")
        assert not guard.blocked("other")
    finally:
        state.close()


async def test_inbound_flood_and_ordinary_conversation_do_not_trip(tmp_path):
    state = module("state").StateStore(tmp_path / "loop.sqlite3")
    guard = module("loop_guard").LoopGuard(state, "a", clock=lambda: 1000)
    try:
        for i in range(100):
            guard.incoming("peer", str(i), "同样的话")
        assert not guard.blocked("peer")
        for i in range(3):
            guard.incoming("other", str(i), ["今天好吗", "几点吃饭", "晚安"][i])
            guard.outgoing("other", f"out-{i}", ["挺好", "十二点", "好梦"][i], trigger=str(i))
        assert not guard.blocked("other")
    finally:
        state.close()


async def test_adapter_associates_repeated_replies_with_real_incoming(serve, tmp_path):
    adapter, queue, msg, sent = await make_adapter(serve, tmp_path)
    try:
        for i in range(3):
            msg.update(id=f"in-{i}", serverId=str(900 + i), body="hello")
            await adapter.process_native_event(native_event(msg, str(i + 1)))
            await queue.get_nowait().send(MessageChain([Plain("hi")]))
        assert len(sent) == 3
        assert adapter.loop_guard.blocked("peer")
        msg.update(id="in-4", serverId="904")
        await adapter.process_native_event(native_event(msg, "4"))
        assert queue.empty()
        assert adapter.state.cursor == "4"
    finally:
        await adapter.terminate()
