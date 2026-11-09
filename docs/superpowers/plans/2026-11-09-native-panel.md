# WeChat Native Panel Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement the tasks in this session.

**Goal:** Deliver the approved compact panel with persistent seven-day statistics.

**Architecture:** Journal newly received message IDs with their local reception date in the existing SQLite transaction. Read daily aggregates through a separate read-only connection; reuse current message outcomes rather than maintain duplicate counters. Render the panel with native HTML and SVG.

**Tech Stack:** Python, SQLite, AstrBot Plugin View bridge, HTML, CSS, JavaScript, SVG, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-09-native-panel-design.md`

## Global Constraints

- Keep existing connection routes and saved-robot selection.
- No added dependencies or unrelated changes.
- Never initialize StateStore or an adapter while reading panel data.
- Daily results use the server's local reception date and current message status.
- No credentials, chat contents, or paths in API results.

## Review Focus

- Replayed and outgoing events must not inflate received totals.
- Segmented and partial sends must not inflate successful replies.
- Legacy, missing, or unreadable journals must not be mutated or shown as valid zeros.
- Separate robot selection must clear stale data, including on request failure.
- Mobile, dark mode, keyboard chart details, empty data, and refresh failures must remain usable.

### Task 1: Persistent statistics and read-only panel API

**Files:** `state.py`, `adapter.py`, `connection_view.py`, `main.py`, `tests/test_panel_statistics.py`, `tests/test_connection_view.py`, `tests/test_adapter.py`.

**Interfaces:** `state_path(root, api_url, account_id, bot_id)` shares the existing journal identity. `read_statistics(path)` returns seven daily aggregates, today, pending review and collection metadata. `GET connection/panel?bot_id=...` adds safe statistics and configuration.

- [x] Add failing tests for deduplication, cross-day results, restart recovery, legacy/read-only access and robot isolation.
- [x] Run those tests; expect missing statistics API failures.
- [x] Implement date tracking in the existing accept transaction; return read-only aggregates and configuration.
- [x] Run state, adapter and connection API tests; expect all pass.

### Task 2: Compact dashboard and verification

**Files:** `views/connection/index.html`, `views/connection/style.css`, `views/connection/app.js`, `.astrbot-plugin/i18n/zh-CN.json`, `README.md`, `tests/test_connection_ui.py`.

**Interfaces:** Consume `connection/panel` independently of Native connection health; preserve `connection/bots` and `connection/check`.

- [x] Add failing browser tests for counters, chart, unavailable data, switching robots, auto-refresh and responsive layout.
- [x] Implement the approved small cards, SVG grouped bars, accessible details and collapsed configuration; rename the view.
- [x] Run relevant pytest files, Ruff checks, and inspect desktop/mobile light/dark screenshots; expect passes without overflow.
- [x] Review the complete diff for unintended state changes and verify the final result.

## Review results

An independent read-only review confirmed the journal safety and counting approach. Fixed both reported issues with regression tests: retain a focused chart day during auto-refresh; distinguish unreadable statistics from an uninitialized journal. Legacy pending-review messages remain included in the all-date backlog by design.

Verification: state/adapter/API checks passed (125 tests); expanded focused checks passed (32 tests); desktop/mobile/dark screenshots inspected at `data/panel-preview/`; Ruff lint, formatting and module compilation pass. Final full suite: 633 passed, with one existing AstrBot `audioop` deprecation warning. Output is recorded at `data/panel-test-run-final.txt`.
