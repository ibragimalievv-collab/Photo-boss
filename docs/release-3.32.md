# Photo Boss 3.32

Web uploads of full-shoot originals now go straight to the private Yandex.Disk shooting folder. A successful response requires provider size and checksum verification and stores the photo row as STORED in the same application transaction. Retries verify existing bytes, preserve one photo record and repair a missing copy. Receipts and attendance photos also use private Disk storage when the app notification channel is enabled. Existing Telegram-origin media remain readable.

Owner: More → Check photos on Disk. This reads every page of the selected folder, including files not registered in the database, and separately counts complete-shoot folders. It shows declared, actual, unique, duplicate, unregistered and missing counts. Selected exports do not inflate the complete-shoot count. Unavailable storage is reported as unverified, never as zero. Cancelled sale drafts remain available to this read-only historical audit. Similar frames and composition remain in the existing AI shoot review; they are estimates, not exact duplicate proof. Original bytes are preserved.

Lunch return remains accessible if an interval crosses midnight. Push respects owner critical-notice preferences; startup records only aggregate subscription/delivery counts, without endpoints or keys. Device acceptance still requires permission and a real device test.

## Scoped owner-requested reset

The user explicitly requested clearing current sales and fines on 19 October 2026. The new startup-only request uses `PHOTO_BOSS_SALES_FINES_RESET_REQUEST` with `{ "requestId": "unique-request-id", "scope": "sales-and-fines" }`. This is a separate operation from the broader historical reset.

Under exclusive database locks it writes and verifies a typed immutable archive before changing anything. It removes sales, their feedback and linked manager commissions and fines; cancels sale drafts; clears monetary attendance fines while retaining actual arrival time and late history; reopens previously sold bookings; rejects pending payment receipts attached to reset sales. Approved payments, bank records, original files and their metadata, attendance, lunch, schedules, unrelated payroll and chats survive. Every unrelated table and previous audit row is verified before commit. Any mismatch rolls back the entire transaction.

A stable request ID is idempotent across restarts and cannot erase later work. An independent sales generation blocks stale queued sale packets without invalidating attendance/lunch packets. The archive is version 2 with scope `sales-and-fines`; the legacy full-history restore refuses it. Restoration must use a scope-aware recovery procedure after checking for newer sales; do not pass this archive to the legacy reset restore.

Bank acquiring remains disabled/unmodified.
