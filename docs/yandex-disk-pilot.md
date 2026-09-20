# Yandex.Disk pilot storage

Photo Boss uses the narrow OAuth scope `cloud_api:disk.app_folder` and only
addresses `app:/PhotoBoss/...`. The OAuth token is read from the Render
environment variable `YANDEX_DISK_TOKEN`; it is never returned to the Mini
App, committed to Git, or intentionally logged.

At startup the pilot verifies the app-folder connection and write capability by
creating the server-owned `app:/PhotoBoss/system` directory, uploading a tiny
`connection-check.txt`, reading its metadata, and deleting it. A failed Disk
probe does not take Telegram/finance/attendance offline; the storage status is
reported as degraded.

Owner-only endpoints:
- `GET /api/miniapp/storage/status`
- `POST /api/miniapp/storage/probe`

This release is only the storage foundation. It does not yet upload guest
shootings or send images to an AI provider. Those flows require explicit
shooting authorization, persisted upload/analysis jobs, retries, hashes, and
data-handling approval.
