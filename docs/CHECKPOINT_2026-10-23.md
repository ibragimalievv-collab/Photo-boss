# Photo Boss handoff — 23 October 2026, 12:30 UTC

This checkpoint supersedes the deployment/acceptance portions of AUDIT_2026-10-23.md. The module register in that file remains useful. The user asked to move to a new chat and stop new independent changes after this checkpoint. Do not run competing executors or restart the audit from scratch.

## Exact versions and publication boundary

| Item | State |
|---|---|
| Production | **3.45**, release `miniapp-3.45-notifications-practice-recovery` |
| Production main | `2ce6587e5cb36a8ba8b3b2a8d807c0689aa0d96a`, PR #99 merged |
| Production deploy | `dep-dbdkh9m417fc738nqb10`, live 2026-10-23 11:46:13 UTC |
| Production service | `srv-damohb1mirbc73as0rl0`, https://photo-boss.onrender.com/app/ |
| Prepared release | **3.46**, PR #100 **not merged or deployed to production** |
| PR #100 branch | `fix/planned-lunch-hotel-3-46` |
| PR #100 head | `7e02abfe5a9b6374fd611759a1c670dba179e01c` |
| Isolated live QA branch | `qa/isolated-live-acceptance-2026-10-23` |
| QA deployed source | `527402598c18cf5b649c96e1d0882927e688b376` |
| QA deploy | `dep-dbdl33qvcj2c738593e0`, live 2026-10-23 12:24:21 UTC |
| QA service | `srv-dbdkq8uq1p3s738v8p3g`, https://photo-boss-isolated-qa-20261023.onrender.com/app/ |

Repository: https://github.com/ibragimalievv-collab/Photo-boss

Production auto-deploys main. Do not trigger a duplicate deployment after a future merge. QA has auto-deploy disabled: publishing this documentation does not reset its test database.

## Current release gate: one CI job did not complete

At PR #100 head, 15 of 16 check runs succeeded. The remaining `test` job was cancelled at approximately its configured 15-minute job budget (12:13:27–12:28:43 UTC). GitHub's exposed conclusion is `cancelled`, not a successful or completed smoke check. The available logs do not identify the actor/reason for cancellation beyond `The operation was canceled`; the annotation endpoint is outside the connected tool's allowed endpoints. Do not present a more specific cancellation cause as proved:

https://github.com/ibragimalievv-collab/Photo-boss/actions/runs/39951547503/job/120407484108

Verified log sequence:

- Docker stack successfully built and became ready.
- Static/Node checks passed; **1154 pytest tests passed**.
- At 12:17:18 UTC CI started `docker compose ... exec -T bot python -m app.smoke`.
- At 12:17:22, `INFO:app.db:PostgreSQL connection is ready` was emitted.
- No smoke success followed. At 12:28:41 the operation was cancelled; cleanup completed.
- The following additive-migration step was skipped.

The exact blocking SQL/transaction is **not yet established**. Do not label this as only a slow network issue or a proven PostgreSQL deadlock without diagnostics. `app.smoke` calls `wait_for_database()` then `init_db()` against an already-running application. Next engineering step: reproduce/diagnose that repeated initialization with bounded SQL timeouts and PostgreSQL blocker/transaction diagnostics on an isolated stack, then fix the cause and finish PR #100 checks. Do not bypass the check, claim green CI, or merge/deploy 3.46 on this evidence.

An additional push-triggered `test` check at the same PR head succeeded; this does not negate the timed-out pull-request run. The separate PostgreSQL attendance/HTTP audit, Render-runtime Python suite, browser workflows, calls, TURN fixture, delivery and recovery checks passed. Local full suite also passed: **1154 passed in 91.89 seconds**.

The published QA source `5274025` also completed its automatic CI run **39952825735 with success**. This is further isolated evidence, not a replacement for the unresolved PR run or proof of the specific smoke blocker. At the handoff check there were no remaining CI runs executing for that QA commit, and the local pytest and local temporary QA-server processes had exited.

## Confirmed in production 3.45

- Existing signed-in workspace loaded and footer showed 3.45. The available real account combines four roles; this is not four independently authenticated production users.
- Attendance control list loads both confirmed and pending records. An actual stored attendance photo opened and decoded at 1280 × 1280. No real approval or employee timestamp was changed for testing.
- Booking #45 had a 14:00 appointment and a stored reminder at 13:40:05, including hotel/room/details. Opening the notification opened the correct booking. Unread count changed 18 → 17 and stayed 17 after reload.
- Enabled Telegram copies only for the current authenticated owner's existing Telegram binding. Sent one explicit notification test. App later displayed **accepted by Telegram; reading not confirmed**. No mass employee test notifications or arbitrary recipients were used. Historical notifications were not sent retrospectively.
- Academy assignment #4 already had a reshoot decision for slots 1/3. Real storage 404s were confirmed for slots 2/4/5. The production recovery action reopened only those 404 slots; server log at 11:49:09 UTC recorded `[2, 4, 5]`. Refresh showed reupload required, 0/5, all needed upload controls. Old references remain archived; missing bytes were not restored and no fake practice photos were submitted.
- Workday/lunch page loaded, but its hotel default was the first visible hotel rather than the day's planned hotel. This is fixed in prepared 3.46, **not yet production**.

## Confirmed on separately published isolated QA 3.46

QA uses real business HTTP handlers and frontend from PR #100 plus `tests/isolated_live_server.py`. It is an explicit public, disposable synthetic fixture: four QA roles, a synthetic camera stream/GPS adapter, SQLite and checksummed local file bytes. No real employee/client records, Telegram credentials, production database, payment credentials or Disk credentials exist there. External sends fail rather than return fake success. The persistent yellow banner states the boundaries. QA results are not production/device/provider acceptance.

The latest cycle was performed through the browser UI on deployed `5274025`:

1. Manager role → start shift → mandatory GPS → camera → capture. The QA-only visible link injected exactly one photo-upload HTTP 503.
2. App saved the mark locally and showed synchronization pending. Manual synchronization sent the same saved operation; after reload the server status was **start saved, needs review**. It did not pretend that the original 503 succeeded.
3. Lunch allowed for the server-saved pending start. Available hotels were QA Фиолето first and QA Мовенпик second; **QA Мовенпик was selected from the shift plan**.
4. Lunch start persisted across reload. Return created one closed interval: **15:26:20 → 15:26:31 Moscow**, 11 seconds (UI floors this to 0 minutes). Earlier isolated PostgreSQL regression also checked a 31-minute interval.
5. Switched to separate QA owner role → attendance control → pending manager start → opened stored photo. Image decoded at **640 × 800**.
6. Confirmed the test start with an explicit QA reason. The list reloaded successfully; no service-unavailable error. Claimed and verified time remained **15:25:28**, including seconds. In-app confirmation event appeared with employee, hotel, reviewer and reason.
7. Returned to manager role → finish shift → GPS → camera → capture → confirm. An immediate capture before video readiness showed a useful retry message; retry after readiness succeeded.
8. Reloaded final workday: **shift closed**, one closed lunch interval persisted, new lunch was disabled, queue showed synchronized operations.

The QA database also applied its normal lateness rule to the synthetic manager (plan starts 03:00). This affects only synthetic QA accounting, never real payroll.

An earlier isolated live cycle additionally checked normal online start/lunch/return/end and leader correction. Isolated PostgreSQL tests exercise actual PostgreSQL user/FK concurrency, approval replay, counts and transactions. The SQLite QA web service cannot prove PostgreSQL locking.

## Changes waiting in PR #100

- Lunch hotel defaults to the unique planned hotel, preserves explicit selection, and requires a choice when ambiguous. Server scope checks remain in force.
- Attendance review form preserves seconds instead of silently rounding verified timestamps down to the minute.
- More accurate Academy missing-original/reupload text.
- Footer/release 3.46 and service-worker cache update.
- Recovery CI installer no longer redundantly downloads Ubuntu system packages via `playwright install --with-deps`. An earlier run timed out during APT download before application tests; that separate job subsequently passed.

## Reproducible QA scenarios and code

- `tests/isolated_live_server.py`: public QA launcher, seed data, visible role links, one-shot photo 503, DB-only booking reminder loop, integration guards.
- `tests/attendance_postgres_check.py`: PostgreSQL shift/lunch/approval/replay/concurrency acceptance.
- `tests/app_postgres_audit.py`: **156 HTTP role/JSON checks** on a disposable PostgreSQL schema.
- `tests/attendance_gps_only_browser_check.py`, `tests/attendance_no_gps_browser_check.py`, `tests/lunch_recovery_check.mjs`, `tests/attendance_review_route_check.mjs`, and `.github/workflows/recovery-check.yml`.
- `.github/workflows/ci.yml` and other existing workflow files are authoritative for exact script names/commands; do not infer a test exists just from the list above.
- Remaining modules and prior isolated tests are enumerated in `docs/AUDIT_2026-10-23.md`.

To resume the live QA cycle, use the visible role selector. A closed synthetic shift already exists for the manager on 23 October. Do not delete/modify real production records to replay this. QA storage is ephemeral and can reset on a deploy/restart; a reset is not production data loss. It is on a free Render service and can cold-start after inactivity.

## Remaining acceptance, access and user steps

- Production full write cycle was **not** performed under a separate test identity. The new QA service removes the lack of a safe test environment, but is deliberately isolated. Do not claim that its synthetic GPS/camera prove a real phone.
- Physically verify production camera/GPS permissions and actual background notification display with the app closed on the user's Android/iPhone. Provider acceptance is not phone display or reading. Cross-device read state has isolated coverage, not observed second-device acceptance.
- Academy completion needs actual replacement originals/reshoots. Opening upload slots cannot reconstruct absent files or establish image quality/pose accuracy.
- Full booking → photographer assignment → original upload → receipt/deposit verification → final sale was covered by existing isolated automated suites, **not newly completed in this QA UI cycle or production**. No new booking or sale was created in this live QA session. Continue this independent acceptance after the checkpoint/release gate when appropriate.
- Production TURN remains unconfigured; bank not configured. Real mobile-network calls, a bank payment and a real microphone/live AI-provider session remain unaccepted. Startup/configured status is not a successful end-to-end provider operation.
- Production SQL connector is blocked by its external allowlist. Do not weaken network isolation to get diagnostic access.
- Telegram has no send idempotency key: a process loss after provider acceptance but before recording acceptance can duplicate a copy. Committed ACCEPTED deliveries are not normally resent.

## Local work / portable source status

Local workspace at handoff: `/workspace/scratch/dcf33376fc7c/photo-boss-audit`.

Git status still shows 27 modified/untracked production-source/test/report files relative to the old local base. Their Git blob hashes were compared with PR #100's published tree and **all 27 matched exactly**. They are not unpublished work. The only additional source file, `tests/isolated_live_server.py`, is published in QA commit `5274025` and intentionally absent from production main. This checkpoint itself is committed on the QA branch.

Temporary pytest logs, synthetic JPG upload fixtures and browser state are scratch-only, not required source or credentials. The sales fixtures were prepared but not used in a new live sale. There is no required code available only in scratch. The older sibling `photo-boss` checkout contains stale 3.44 work; use GitHub commits above, not that checkout.

No new production deployment or manual CI rerun was initiated after the timed-out check. The current release is safely held at PR #100. Do not interpret an old deployment-in-progress message as the current production state.
