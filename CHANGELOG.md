# Changelog

## 0.3.13 — 2026-11-12

- Make explicit QR retries bypass saved-session recovery; persist the request strategy with its idempotency key across timeouts and restarts.
- Remove temporary QR login diagnostic logs after validating the login flow.
- Center the QR login card content on desktop and mobile layouts.
- Add Native v2.3.0 QR login to the existing connection panel, using the existing Admin Token, durable idempotency and validated in-memory PNG transfer.
- Add administrator private-chat alert commands with Chinese aliases and platform-neutral routing through AstrBot.
- Add independent read-only connection monitoring, durable 3-failure / 2-recovery incidents, and at-most-once notification attempts across restarts.
- Preserve the existing message pipeline, statistics and 30-second panel refresh; add mock HTTP, incident, permission and Chromium coverage.
