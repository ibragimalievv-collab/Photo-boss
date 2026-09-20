# Photo Boss · Mini App v3 test release

## Scope and data

This pilot uses the existing bot database. No demo users, sales, receipts, financial entries or balances are seeded. The public JavaScript bundle does not contain a demo mode or a role switch. All API requests require valid, recent Telegram initData and an active registered staff account.

The authenticated app is served at `/app/` on the bot's existing Render service. `/app` redirects there. In Telegram send `/start`, then `/app`, or use the updated chat menu button. BotFather's separate Main Mini App URL is not the same setting as the chat menu; an old profile Launch button may still lead to the previous chatgpt.site frontend until the owner updates that separate setting.

## Included

- Three independently saved workspace themes; theme never grants a role.
- Today's booked shoots, planned staff with check-in status, actual sales ledger and approved receipt totals.
- Schedule reads and explicit create/cancel actions. Started/past shifts cannot be cancelled. Overlaps are rejected.
- Separate black-and-gold Academy: lessons, practices, saved mentor reviews, recorded issues, achievements, reference gallery and profile.
- Persisted and idempotent lesson completion.
- Owner-only paginated audit with actor, action, entity and all retained details. Unknown events display their actual action code, not an opaque 'service action'.
- Server permissions: owner all financial periods; administrator company totals only today; other staff own credited finances only. Old administrator report/date/FSM shortcuts are redirected to the protected app. Legacy audit shortcuts cannot reveal the log to an administrator.
- Protected newly sent Telegram content, no-store API/static responses, a secret-verified webhook, per-user watermarks and limited browser copy/print deterrence. None is a guarantee against screenshots or photographing a display.

## Explicit limitations

- This is a test release, not a promise that all functions shown in a presentation are integrated.
- AI photo analysis is not enabled. No API purchase is made. Displayed ratings are saved mentor reviews, not generated guesses.
- Booking creation, sales, receipts, check-in and practice upload continue in the existing Telegram bot via explicit handoff. Forms are not silently simulated.
- Financial amounts reflect existing stored accounting records. The Mini App does not replace or repair the pre-existing payroll engine. Before real payroll, verify photographer/manager commissions, bonuses, deductions and receipt reconciliation end to end. Accrued amounts are not paid salaries; sales are not received cash; no 'net profit' is inferred.
- Cash means approved receipt amounts on their confirmation date. It is not a certified bank statement or physical till reconciliation.
- The dashboard's team initially follows planned shifts; check-in without a corresponding planned shift is not a complete attendance roster. Inspect the bot's attendance journal when testing.
- Academy totals for reviews/practice cover the returned recent records, not lifetime totals. Only persisted lesson points are counted until unified growth scoring is implemented. Reference images are educational illustrations, not employee portfolios or customer galleries.
- A free Render web service may spin down; first access and scheduled reminder/background work can be delayed. An in-memory bot conversation can be reset on restart if Redis is not configured. No paid resource or Redis service is created by this release.
- Render free PostgreSQL expiration and data backups require an owner check before paid production use. Do not enter real data into external preview copies.

## Validation

The `Mini App release` workflow validates this exact Git commit. Server tests run against an isolated local CI PostgreSQL 16 schema and SQLite. Browser UI tests intercept all requests with fixtures and never contact real Telegram or Render. Main CI runs existing regression, dependency and Docker checks. `qa-release` workflow artifacts identify their commit and contain reports/screenshots.

Only after passing checks should the release branch be merged. Verify the Render deployment SHA and `/health` (release `miniapp-v3-test`, mode `test`), inspect startup logs and Telegram menu installation, then perform a real-phone login as owner. Public `/api/miniapp/me` must return 401; `/app/js/demo.js` and private source paths must return 404. Browser fixture tests do not replace this real-phone test.

## Rollback

Keep the prior commit before merging. For a frontend regression keep financial/audit security guards and disable or correct the app launch separately rather than re-opening old access. No destructive schema migration is part of this release. Never reset the database to fix a UI problem.

## Before paid production use

Owner approval of hosting charges, verified backups, persistent bot state/uploads, actual multi-role phone tests and reviewed payroll are separate release gates. Upgrading hosting alone does not implement AI analysis or the remaining web forms. No automatic charge or scheduled paid upgrade is configured here.
