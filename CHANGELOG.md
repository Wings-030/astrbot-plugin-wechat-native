# Changelog

## Unreleased

- Add redacted, stage-specific QR login diagnostics for Native operation failures, HTTP errors and PNG validation; retain existing login and retry behavior.
- Add Native v2.3.0 QR login to the existing connection panel, using the existing Admin Token, durable idempotency and validated in-memory PNG transfer.
- Add administrator private-chat alert commands with Chinese aliases and platform-neutral routing through AstrBot.
- Add independent read-only connection monitoring, durable 3-failure / 2-recovery incidents, and at-most-once notification attempts across restarts.
- Preserve the existing message pipeline, statistics and 30-second panel refresh; add mock HTTP, incident, permission and Chromium coverage.
