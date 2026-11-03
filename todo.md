# Todo (Playbook: Investigation)

throughput checkpoint: n/a, read-only investigation

- [x] 1. Route through the **how** skill. For motivation questions, also route through the **why** skill.
- [x] 2. Throughput checkpoint stays one line: `throughput checkpoint: n/a, read-only investigation`.
- [x] 3. Produce the `how`-shaped output (Overview / Key Concepts / How It Works / Where Things Live / Gotchas), or a recommendation with a tradeoffs table if the request is a decision between alternatives.
- [x] 4. Apply the **unslop** skill to the reply.

## Task-specific steps
- [x] 3a. Spawn 3 read-only `how` explorers in parallel across plugin lifecycle/config/state/UI, protocol client (`native_client.py`), and adapter/event/summary pipeline (`adapter.py`, `event.py`, `summary.py`).
- [x] 3b. Spawn 1 read-only `how` explainer to synthesize explorer findings.
- [x] 3c. Present the synthesized architecture explanation after `unslop` pass and remove temporary `todo.md` if desired or leave uncommitted per poteto-mode.
