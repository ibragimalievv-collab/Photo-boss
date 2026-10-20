# Photo Boss 3.37 — private Disk folder audits and fixed booking prices

Prepared changes; publication/deployment and real-account authorization must be verified separately.

## Read-only Disk folders

- Owner can browse the connected account's `disk:/` folders, choose one and optionally link its audit report to a booking.
- The worker lists nested folders, then reads original bytes in resumable batches. No photo copy is uploaded and no source is deleted or edited. Existing app-folder write restrictions remain in place.
- A report separates listing progress, readable/failed originals, unique checksums, exact copies, perceptually similar candidates, per-frame AI results and unavailable AI batches. An incomplete listing or unreadable file is never reported as a complete quality audit.
- Provider 401/403, account-token changes, owner-role revocation, interrupted workers and limits (1000 photos / 200 folders per audit) have explicit outcomes. Reports persist across browser reloads. RAW/HEIC files without a supported decoder remain counted but visibly unverified.
- The existing server token is reused for reads, or `YANDEX_DISK_READ_TOKEN` can supply a separately authorized read-only account. Reading `disk:/` requires the connected account's actual Yandex permission; changing code does not grant OAuth scopes or access other accounts.
- The report does not register an external folder as a completed original upload, change sale totals, commissions or penalties, or create a guest gallery. These business transitions remain explicit.

## Fixed prices and deposits

- A supervisor saves one current UNIT rate per hotel. Each new booking receives an immutable price snapshot; changing a package or later hotel tariff cannot rewrite that booking's unit price. Existing counted drafts and purchases retain their own snapshots.
- Previously auto-seeded 300/400/500 alternatives are not a fixed rate. A hotel with ambiguous alternatives requires a supervisor's choice; no latest-created or arbitrary price is applied. No actual hotel prices are assigned by this release.
- Booking deposit defaults to 1000 RUB and accepts another finite nonnegative amount with no more than two decimal places. Historical deposits are not mass-updated.
- Only approved actual payments reduce the outstanding balance, once across the whole booking. Partial deposit collection correctly remains due. Sale revenue stays gross; overpayments remain visible and do not create a negative debt.
- UI previews and the bot's booking entry follow the same defaults. Offline cold-start caching includes the new module.

## Verification

- External Disk read tests cover recursion, duplicates, broken images, read scope refusal, revoked owner access, account changes, stale worker recovery, limits and unavailable AI. Provider calls are simulated; they do not prove access to the production account's root.
- Real Chromium workflow check covers cold offline start, receipt 503 isolation, retry/replay after a lost response, one sale/commission, offline camera shifts and owner review.
- Final repository checks and remote CI results are recorded at publication time.

## Local acceptance results

- `DATABASE_URL=sqlite+aiosqlite:///:memory: python -m pytest -q --disable-warnings`: **1065 passed** (merged with upstream storage recovery).
- `ruff check app tests` and `git diff --check`: passed.
- Client checks for Disk folder browsing/reports, fixed-price/deposit previews, notifications and sales-history escaping: passed.
- Chromium `workflow_browser_check.py`: passed cold offline startup, receipt-error isolation/recovery, lost-response replay without a second sale or commission, camera shift capture and owner review.
- Production-account root-folder access, real photographic AI results and push delivery to an actual employee phone remain unverified. No source folders have been scanned against a live account by these local tests.
- Release 3.37 is not published or deployed by this work; activation and remote CI remain pending.
