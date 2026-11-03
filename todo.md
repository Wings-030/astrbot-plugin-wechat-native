# Todo (Playbook: Feature — WeChat Native Image Recognition)

- [x] 1. `how` over the affected subsystem.
- [x] 2. `architect` for parallel design exploration.
  - [x] 2.1 Ground
  - [x] 2.2 Sketch
  - [x] 2.3 Agree
  - [x] 2.4 Implement
  - [x] 2.5 Scrap
- [x] 3. Write the throughput checkpoint as four todo items:
  - [x] **Blocking first steps.** Ground `agent-wechat v1.3.3` Media API contracts (`native.generated.ts`, `media.rs`, `native-media.ts`, `native-quote.ts`) and synthesize the `architect` type/signature sketch before touching code.
  - [x] **Independent workstreams.** `config.py`, `native_client.py`, `event.py`, and `adapter.py` are tightly coupled along one message pipeline (`NativeConfig` -> `NativeClient` -> `convert_message` -> `WeChatNativeAdapter`), so code-coupled work goes to a single sequenced stream after the parallel `architect` design bakeoff.
  - [x] **Shared mutable state.** `StateStore` (`state.py`) and `self._events` / `self._send_tasks` / `self._media_tasks` in `WeChatNativeAdapter` share per-message lifecycle state (`queued`, `recalled`, `no_reply`, `replied`, `failed`, `needs_review`). Deduplicate via `self.state.accept(...)` before spawning any async media task, bound concurrency with `asyncio.Semaphore(3)`, and serialize all outbound replies through `self.send_chain` under `self._send_lock`.
  - [x] **Smallest safe decomposition.** One sequenced implementation stream ordered by `principle-sequence-verifiable-units`: (Unit 1) `config.py` + `event.py` + unit tests, (Unit 2) `native_client.py` media endpoints/validation + unit tests, (Unit 3) `adapter.py` async media pipeline, recall/error handling, and `README.md` + integration tests.
- [x] 4. Delegate code-writing to a subagent using your configured feature model with a specific scope.
- [x] 5. Verify on the matching surface. "Inconclusive" or wrong-surface is not a pass. Flag it.
- [x] 6. Rebase into small, ordered commits. Stack follow-ups. (skip: user did not request git commit)
- [x] 7. If the design is contested, `interrogate` before shipping. (skip: design converged cleanly on Candidate A with grafted Candidate B safeguards)
- [x] 8. Run **Opening a PR**. (skip: user did not request opening a PR)
