# 3.53: selected-gallery Disk work outside the application pool

2026-10-30: reproducible defect found in gallery photo selection. Its transaction
held a Booking FOR UPDATE lock and a pooled DB connection while reading the
original and copying/deleting the selected Disk file. Parallel selections could
therefore occupy pooled slots while awaiting a slow provider or the booking lock.
This matches a possible mechanism for the 2026-10-29 05:55 UTC pool timeout;
historical production attribution is NOT proven by this reproduction alone.

The handler now uses short preflight/revalidation/final transactions and releases
the application connection before every provider wait. Per-booking advisory locks
on the existing separate unpooled engine serialize select/deselect across Render
instances. Admission is bounded to four operations per process, with retryable
503 responses before opening a selection lock when busy. Same-value replay does
not repeat copying; provider failures do not mark DB state saved. Original
identity and current employee role/assignment are revalidated before provider
writes and before the final acknowledgment. Existing reset barriers remain.

Tests include pool checkout observation during a stalled provider, role revocation,
exact-value retry, existing delivery suite and real-PostgreSQL selection/replay
with the two-connection fixture in tests/delivery_download_postgres_check.py.
The pool regression failed on 3.52 before implementation.

Current production pg_stat_activity cannot be read with the connected Render
query tool: database external IP allowlist is empty. It was not weakened.
No real guest photos, financial records, shifts or storage credentials were
changed for testing. Synthetic provider I/O does not establish actual phone/Disk
acceptance or eliminate all other sources of pool exhaustion.
