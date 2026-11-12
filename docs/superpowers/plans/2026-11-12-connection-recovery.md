# Connection recovery implementation plan

**Goal:** Implement the approved QR login and cross-platform connection alerts.
**Spec:** `docs/SPEC-wechat-native-connection-recovery-v2.3.0.md` (revision 2.0).
**Architecture:** Share a read-only probe between the existing panel and an independent monitor. Keep login sessions, notification bindings and incidents in AstrBot Plugin KV; use public AstrBot APIs at integration boundaries.
**Tech stack:** Existing Python/aiohttp/AstrBot and HTML/CSS/JavaScript; pytest, Ruff, Playwright.

## Global constraints

- Native contract baseline: tag `v2.3.0`, `/login-attempts` routes only.
- Reuse `admin_token_file` and `token_file`; no new user settings or secrets.
- Monitor: 60 seconds, 5-second probe, 3 failures / 2 recoveries.
- Mark notification attempted durably before sending; never retry an incident event.
- Administrator private chats only; Dashboard authorization must fail closed.
- Preserve existing message, journal, statistics and tools behavior.

## Review focus

- Lost POST responses and KV failures must preserve the login idempotency key.
- Stale QR generations and bot-switch responses must never display another QR.
- Missing or stale authorization must fail closed without affecting read-only checks.
- Binding changes and disabled/reconfigured robots must not replay old incidents.
- Cancellation and individual bot failures must not stop other bots or leak tasks.

## Tasks

### 1. Shared probe and Native login transport
- [x] Add failing mock HTTP tests for strict health classification, v2.3.0 requests and PNG validation.
- [x] Implement `probe_connection(robot)` and NativeClient login-attempt methods.
- [x] Verify targeted pytest; preserve legacy connection response fields.

### 2. Durable login and privileged Web API
- [x] Add failing tests for concurrent starts, lost responses, restart, terminal retry, identity changes and authorization.
- [x] Implement `LoginSessionManager`, per-bot locking and KV validation; register start/status/qr APIs.
- [x] Verify targeted pytest against real AstrBot PluginRequest/Plugin KV interfaces.

### 3. Cross-platform commands and monitor
- [x] Add failing tests for trusted private events, aliases, target selection, routing, incident thresholds, durable attempts and lifecycle.
- [x] Implement binding/sender/monitor services and plugin initialize/terminate hooks.
- [x] Verify targeted pytest including failed persistence, rebind, restart and concurrent checks.

### 4. Existing Dashboard QR flow
- [x] Add failing Playwright tests for scan/approval/success, manual retry, generation change, visibility and bot switch races.
- [x] Extend existing panel via the Bridge; keep statistics and 30-second refresh.
- [x] Verify all existing and new panel tests in Chromium.

### 5. Documentation and final verification
- [x] Update README, operations, changelog and test report with actual compatibility evidence and manual acceptance limits.
- [x] Run pytest, Ruff, compileall and inspect the whole diff; obtain a fresh review.
- [x] Commit verified changes on `feat/native-qr-login-cross-platform-alerts`; report exact results and remaining real-account acceptance.

## Execution record

- Approved specification supplied with explicit instruction to start development; implement directly in the current checkout on a feature branch.
- Baseline: 97 targeted existing tests passed (connection API, NativeClient and Chromium panel).
- Tasks 1–4 complete: independently reviewed connection/probe/login/alert/Chromium suite: 105 passed.
- Ruling: The pinned host has one Dashboard account and no separate role service. Compare authenticated `PluginRequest.username` with configured `dashboard.username`; deny if missing or mismatched.
- Ruling: Add only the boolean `retry` to the start request so explicit terminal restart is distinguishable from automatic page entry; active/uncertain attempts always reuse their key.
- Native source check found `starting`, `resuming` and `authenticating`; all are now covered by HTTP/session tests.
- Race regressions: terminal UI on bot return; stale QR after account health changes; delayed status read overwriting incident cache. Each reproduced before its fix and passed afterward.
- Independent read-only review complete: no remaining important correctness/security findings; real-account acceptance and distributed locking remain explicit limits.
- Final verification: `python -m pytest --cov=. --cov-report=term-missing -q` → 789 passed; Ruff, compileall and whitespace checks passed. One existing upstream `audioop` deprecation warning.
- Integration choice follows the supplied specification: commit the completed feature branch locally; no merge or remote publication requested.
