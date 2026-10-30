# Bound startup schema transactions

Observed during deploy dep-dbi57i142hec73f8i7a0: new process connected to
PostgreSQL at 08:23:32 UTC; the old process h65bz exhausted its application
pool around 08:29–08:33 and public health returned 502. Release 3.53 became
live at 08:33:59; health returned 200 with fresh Disk read/write verification.
The precise historical blocking relation remains unobserved: external Postgres
queries and dashboard cancellation are not available in this session.

init_db contains repeated ALTER TABLE, constraint and trigger DDL even when
columns already exist. Such statements can wait for ACCESS EXCLUSIVE locks
while retaining earlier locks; ordinary application queries can queue behind
the waiting DDL. The new transaction uses a two-second lock timeout, 30-second
statement timeout and 90-second total budget. A failed startup must roll back
instead of holding a schema transaction indefinitely alongside the old server.
No partial migration is committed, and no security or data-retention settings
are relaxed. This bounds failure; it does not replace a future versioned
migration strategy or guarantee that every deployment will start successfully.

The PostgreSQL workflow initializes an isolated schema, retains a reader on
work_chat_messages, verifies startup fails with 55P03 within the deadline and
releases earlier booking locks, then verifies a clean startup succeeds. Local
SQLite bot regressions: 69 passed. No production records used for tests.
