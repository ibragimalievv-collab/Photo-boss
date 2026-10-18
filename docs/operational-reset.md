# Archived operational restart

An operations reset is a deliberate owner-authorized restart of **all historical**
attendance, bookings, shoots, sales, sale drafts, associated receipt/gallery rows,
explicitly labelled fines and sale-derived manager commissions. It also clears
operational reminders, reports and counters. It does not reset PostgreSQL sequences
or delete media from Disk/Telegram.

Employees, access, hotels, clients, planned shifts, prices, percentages, unrelated
manual payroll entries, actual cash disbursements, Academy, HR and chats remain.
Academy photo-review links are detached and archived; the reviews remain.

## Deploy and execute

1. Ship this release **without** `PHOTO_BOSS_OPERATIONS_RESET_REQUEST`. Verify CI,
   the external health/version and that the preceding release is retired. The
   startup log `Operations reset preview` reports counts and preserved manifests.
2. After reviewing the scope and recovery evidence, configure only this service's
   `PHOTO_BOSS_OPERATIONS_RESET_REQUEST` with a unique request and the currently
   observed generation (`""` before the first reset):

   ```json
   {"requestId":"owner-reset-20261018-unique-id","expectedGeneration":"","scope":"all-operational-history"}
   ```

3. Redeploy the **same verified release**. Startup requires an active OWNER among
   configured `ADMIN_TELEGRAM_IDS`, drains operational leases and commits a durable
   maintenance marker. New operations then receive a retryable response.
4. Under exclusive PostgreSQL advisory/table locks, the reset writes an immutable
   typed snapshot into `operational_reset_archives`, reads back its SHA-256,
   deletes the reviewed graph, verifies zero remaining operational rows and exact
   preservation manifests, writes the new generation and removes maintenance in
   the same transaction. A failure rolls back all deletions and keeps maintenance.
5. Check `Operations reset verified`, external health, `Recent sale audit`, and the
   next worker cycle. Opening an updated app reconciles only that account's old
   operational queue/drafts. Old clients are rejected before idempotency replay.

Keeping the completed request configured is safe: a restart verifies the existing
archive and never deletes later work. A different request must name the new
generation, preventing accidental replay of an older approval.

## Recovery

The archive is stored in the persistent database, not ephemeral service storage.
`inspect_archive(engine, request_id)` verifies its checksum and reports its manifest
without exposing private archived rows. Database triggers reject archive updates,
deletes and PostgreSQL truncation.

`restore(engine, request_id, owner_id)` restores the exact reflected columns and
typed values, including original IDs and Academy links. Run recovery during
maintenance with all operational writers stopped/drained. Restoration refuses a
newer reset, new operational work, changed Academy reviews, schema conflicts or
ID collisions; it never overwrites new work. Restoration issues a fresh generation
to invalidate packets created during the empty epoch. Remove the maintenance flag
only after verifying the restored manifest. Preserve the original archive.

The SQLite FK-enabled suite verifies typed restoration, archive immutability,
mid-delete rollback, unknown-FK refusal, unchanged master data and retry after new
work. The isolated PostgreSQL check verifies production locking and ID behavior.
Never run a reset to test production or manufacture test sales on the live ledger.
