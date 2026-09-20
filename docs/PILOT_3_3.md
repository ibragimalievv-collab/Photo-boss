# Photo Boss 3.3 — staff pilot, not full production completion

This release adds native staff administration to existing Mini App 3.2. It does not overwrite the attendance implementation, finance, Academy or Telegram authentication. It creates no extra database, seeds no employee, sends no real invitation, and does not enable paid services.

## Implemented
- Owner/admin staff list with pagination, creation by numeric Telegram ID, name, multiple roles/hotels, update and access disable/restore. Existing Telegram ID produces an explicit conflict; no silent overwrite.
- Actual server authentication/authorization, mutation-time active/role checks, stable row lock ordering, optimistic revision check, protected owner/self/admin records, audit before/after details.
- Central Documents screen containing a DRAFT general agreement with the explicit owner-work-chat access clause at normal readable size. No separate chat checkbox or recurring banner. It is NOT a data-processing consent and NOT a signed services contract. There is no signing endpoint or automatic acceptance.

## Not implemented by this release
- Internal general/direct chats (issue #10), retention and owner-access controls.
- Invitation claim/signature/contract generation; operator identity, country, employment/contractor status, commercial terms and legal review are missing.
- Yandex Disk OAuth/file queue or automatic Academy AI analysis. No keys, billing or data transfers configured here.
- New booking/sale/payment forms; the existing bot remains the operational path for those actions.

## Contract/consent gate
Do not activate a services contract merely by logging Telegram ID + a click. Confirm the parties, terms and electronic signature method. The app rules may include owner access to work chats in the body of the general agreement, but must accurately inform users; no hidden fine print. If Russian law applies and processing relies on consent, Article 9(1) of 152-FZ requires that consent be separate from other signed information/documents. Do not use services agreements to disguise employment (TK RF Article 15). Seek jurisdiction-specific review before enrollment.

References verified for planning:
- https://www.consultant.ru/document/cons_doc_LAW_61801/6c94959bc017ac80140621762d2ac59f6006b08c/
- https://publication.pravo.gov.ru/document/0001202506240021
- https://core.telegram.org/bots/webapps
- https://yandex.ru/dev/disk

## Morning pilot check
1. Close the old Mini App and open /app from the existing bot. Check the release on /health.
2. Owner: open Staff. Use real authorized accounts only. Existing staff cards display roles and hotels. Do not create fake payroll/sales for testing.
3. In a disposable test environment verify create, duplicate ID conflict, stale revision, admin restriction, inactive account, no loss of history. Do not disable an actual employee merely to test access.
4. Recheck native shift start/end (permission, location, photo, server confirmation) on the real phone; automatic tests cannot prove actual GPS/geofence accuracy.
5. Documents must say DRAFT and cannot be signed. No new employee should be represented as hired/contracted by this pilot.

Rollback: revert application commit without deleting staff/audit records. No destructive schema change is part of this release. Free Render and database limitations remain until the owner separately completes the hosting upgrade.
