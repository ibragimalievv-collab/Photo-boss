# Photo Boss 3.36 — storage probe recovery

Prepared on 2026-10-20 after the live 3.35 deployment exposed a degraded Yandex Disk probe: directories and write/hash verification succeeded, while the read check failed.

## Confirmed defects

- The full startup probe hid the failing stage behind a generic warning. Existing logs therefore cannot establish the cause of that particular provider failure.
- The photo sync loop did not rerun the full probe when `connected=true` and `readVerified=false`; its state could remain degraded indefinitely after a transient read failure.
- A directory-only connectivity check cleared previously established write/read flags even though it did not test either operation.

## Changes

- Report only safe error classifications and the failed stage; exclude provider URLs, tokens, payloads and arbitrary exception messages.
- Retry a transient failure obtaining a download link within the existing bounded read retry. Do not retry permanent authorization errors as transient failures.
- Recheck degraded configured storage in a separate cancellable task at five-minute intervals. Use a unique temporary probe file and remove only that file. Do not replay sales or modify financial records.
- Preserve established full-probe results during successful directory-only checks. A failed connectivity check invalidates those results.

## Verification

- 63 focused storage tests passed, including safe failure diagnostics, bounded retries, retained full-probe evidence and recovery cancellation.
- Ruff passed for all application and test sources.
- The recovery task is started and cancelled in both webhook and polling runtimes. It is recovery for a known degraded state, not continuous monitoring of a healthy connection.

## Acceptance limits

A successful health probe confirms the test file only. It does not confirm receipt uploads, a particular original, pending Academy work, sale draft completion or a photographer's phone notification. Those require an authenticated live account and the relevant records/files.
