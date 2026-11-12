# Native v2.3.0 connection recovery verification

Date: 2026-11-12. Specification: [revision 2.0](SPEC-wechat-native-connection-recovery-v2.3.0.md).

All automated Native and platform interactions use mocks or isolated loopback HTTP servers.
No real WeChat account, token, QR code or external notification destination was used.

## Contract evidence

- Native tag `v2.3.0`: [HTTP route table](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/native-client-rust/crates/wechat-api/src/http_route_table.rs)
  and [generated contracts](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/shared/src/native.generated.ts).
  Start requires HTTP 202; status/PNG require HTTP 200. All login requests use `/login-attempts`.
- The fixed tag's [login worker](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/native-client-rust/crates/wechat-core/src/runtime.rs)
  returns `starting`, `resuming`, `authenticating`, `awaiting_scan`, `awaiting_approval` and terminal states.
  The [QR route](https://github.com/thisnick/agent-wechat/blob/v2.3.0/packages/native-client-rust/crates/wechat-api/src/lib.rs)
  validates an integer generation and uses HTTP 409 `login_challenge_superseded` for stale generations.
- AstrBot installed from the exact revision in `requirements-dev.txt`:
  `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`.
  Reviewed `AuthService.login`, the authenticated plugin API router, `PluginRequest`,
  `PluginKVStoreMixin`, `Context.get_platform_inst`, `Context.send_message`, and command registration.
  This host has one configured Dashboard login identity; privileged handlers compare the
  authenticated request identity with the configured administrator and deny missing/mismatched identities.

## Automated coverage

| Spec scenarios | Tests |
| --- | --- |
| QR-01, strict v2.3.0 request/body/status/token | `test_connection_recovery.py`: Native HTTP contracts and pre-QR states |
| QR-02, scan/approval/success/account confirmation | `test_connection_ui.py`: complete QR flow |
| QR-03, ineligible health | Shared probe, authenticated reconnect and 401/403/503 start rejection; UI eligibility |
| QR-04, PNG/generation validation | Non-PNG, corrupted/oversized image, redirect/409, generation replacement and stale generation |
| QR-05, concurrency/restart/lost response/terminal retry | Login Session tests using persistent fake KV and isolated Native HTTP |
| QR-06, bot switching and statistics | Chromium stale QR responses, terminal task return, existing panel/statistics regression suite |
| QR-07, authorization/configuration | Real AstrBot PluginRequest wrapper; missing/mismatched Dashboard user, disabled/unknown bot, injected/duplicate parameters, missing Admin Token |
| CMD-01/02, aliases and target choice | AstrBot command management list plus one/multiple/disabled Native selection |
| CMD-03/04, trusted platforms and permission | Real AstrMessageEvent values for Telegram, weixin_oc, LINE, OneBot; admin/private rejection and redacted status |
| CMD-05/06, binding/send behavior | Idempotent bind, per-bot unbind, preserved origin, invalid target, false/exception send results, unchanged incident storage |
| MON-01–04, incidents and restart | 3/2 thresholds, opposite-observation resets, baseline, restart and subsequent incident |
| MON-05/06, durable attempts | Persist-before-send, failed write/send, no-binding/rebind, identity reset, disabled/removed robots |
| MON-07, lifecycle and isolation | Parallel per-bot checks, slow-bot isolation, disable during probe, repeated initialize and cancellation/await |

The start endpoint additionally accepts only the boolean `retry` control alongside `bot_id`.
Automatic page entry omits it; the explicit retry button sends `retry: true`.
Active or uncertain tasks always retain their original idempotency key.

## Verification commands

Run from the repository root with the existing development environment:

```powershell
.venv/Scripts/python.exe -m pytest --cov=. --cov-report=term-missing -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/python.exe -m compileall -q main.py connection_view.py native_client.py config.py connection_probe.py login_session.py alert_binding.py alert_sender.py connection_monitor.py
git diff --check
```

Final results: **789 passed**, including **20 Chromium panel tests**. Ruff, the compile command,
and `git diff --check` passed. One existing upstream warning remains: AstrBot's `audioop`
dependency is deprecated ahead of Python 3.13. No real-account delivery result is implied.

Independent read-only review found no remaining important correctness/security issues.
Its separate command:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_connection_recovery.py tests/test_connection_alerts.py tests/test_connection_view.py tests/test_connection_ui.py -q -p no:cacheprovider
```

Result: **105 passed**, one upstream `audioop` deprecation warning.
The review and added regressions covered stale QR eligibility after an account becomes online,
delayed status reads overwriting the incident cache, and native pre-QR states omitted from the summary contract.

## Real-account acceptance: not run

- Native v2.3.0: scan a real QR, approve on a phone, confirm account `online` and intact statistics.
- AstrBot Dashboard: verify authenticated administrator access through the actual hosted Bridge and reject unauthenticated/nonmatching identities.
- For each actual Telegram/weixin_oc/QQ OneBot/LINE instance: administrator private-chat bind,
  Chinese alias or equivalent English command, explicit test notification, and confirm reception on the platform.
- Disconnect for three checks, reconnect for two, repeat another incident; confirm one outage and one recovery attempt per incident.
- Restart AstrBot and reopen multiple browser tabs; verify the original login operation and incident state are retained.
- Verify platform-specific proactive-message quotas and context validity. Adapter API success alone does not establish delivery or read receipt.

Only one active AstrBot plugin instance is supported; distributed locking/CAS is outside scope.
