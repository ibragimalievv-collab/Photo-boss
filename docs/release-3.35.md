# Photo Boss 3.35 — pricing, reminder expiry and Academy scope

Prepared on 2026-10-20. This report distinguishes automated verification from live account acceptance.

## Confirmed problems and fixes

- Legacy/bot sale requests without an explicit hotel tariff selected the most recently inserted unit tariff. Seeding choices 300/400/500 made 500 an unintended default. These requests now use the booking's selected package rate; explicitly chosen hotel tariffs and immutable completed-sale snapshots remain supported.
- A reminder created before a shoot could be retried after its start time. Each delivery attempt now checks the 20-minute window. Provider TTL ends at the shoot time, the service worker suppresses expired messages, and foreground toasts skip expired events. The inbox retains the reminder with an explicit expired status.
- Academy practice access treated every administrator as global. Assignment details, private media and the pending queue now check the administrator's current hotel assignments. An administrator without hotels has no global Academy access. Owner and self access remain available.
- The Avito HR import ordering error that failed release lint checks is corrected.
- Browser recovery checks exposed an asynchronous new-operation reset: a user could select a tariff before an older refresh completed, then lose that selection and have native form validation silently block the next submission. The reset transition now locks the relevant controls while data refreshes and preserves the old form if the refresh fails.

## Local verification

- Full backend suite: 1008 tests passed with the isolated SQLite configuration.
- All source/test Ruff checks, JavaScript syntax checks and whitespace checks passed.
- Independent focused review: 46 tests and the notification client check passed.
- Real Chromium fixture scenario passed: scheduler creates one reminder for the assigned photographer; the app shows its time, hotel, room and delivery status; another user's inbox/read endpoint rejects access; after the start time the inbox keeps the details with an expired status.
- Sale recovery browser checks exercise a failed refresh, a slow refresh, correction of rejected counts and replacement of a duplicate receipt while retaining operation keys and accepted photos. Browser sale fixtures use a disposable file-backed SQLite database so background-task cancellation cannot erase their in-memory database during teardown.
- These checks use test fixtures and do not confirm a real user's phone or production cash records.

## Acceptance still required

- An authenticated real Photo Boss session is needed to verify an actual photographer's reminder and complete a real sale in the live account. Tests using isolated databases do not prove phone delivery.
- Yandex Disk connectivity does not prove that any particular pending original, selected photo or receipt was accepted. Live acceptance must use concrete booking/practice IDs and accessible original files.
- Exact duplicate hashes, file integrity, dimensions and embedded metadata can be checked automatically. Similarity and AI assessments of quality, poses and angles require human review in uncertain cases. Missing or altered GPS cannot establish where an image was taken.
- TURN is not configured in the observed live health response. Real calls across restrictive mobile networks remain unverified.

## Checkly evaluation

The supplied screenshot shows Checkly. It can run scripted browser/API checks continuously and retain failure evidence. Existing Python fixture tests need dedicated JavaScript/TypeScript production monitors rather than direct upload as Browser Checks. Start with a read-only health check and a dedicated test login; transactional checks require isolated test data so they cannot affect real cash, payroll or guests.

No Checkly account has been connected or monitoring deployed in this change. Credentials should be stored through secret management, never committed or sent in chat.

Official references:

- https://www.checklyhq.com/docs/detect/synthetic-monitoring/browser-checks/overview/
- https://www.checklyhq.com/docs/platform/secrets/
- https://www.checklyhq.com/pricing/
