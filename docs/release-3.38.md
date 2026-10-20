# Photo Boss 3.38 — three photo sources in a sale

The sale form now offers device upload, a Yandex Disk link, or selection from a private working Disk folder. A missing closing quote in the sold-count input previously broke adjacent form elements; the markup is corrected and covered by the real-browser flow.

An owner can bind a private folder to a booking. Staff can list/select only the folder of a sale they are authorized to handle, and can attach an existing supported shared Yandex link to that sale. An arbitrary website URL is rejected. The application never publishes private source files or grants Yandex permissions.

Selection reads the original, verifies decoding, bytes and checksums, and saves an opaque reference. No second photo copy is uploaded. Reselecting an already accepted checksum repairs its old media reference without adding another frame or sale. The queued completion follows all selected references. Before sale completion the reference is rechecked for an account change, missing file or altered checksum. Existing prices, deposits, receipt review and single-sale/commission replay safeguards apply. A reference does not finalize full-shoot upload or authorize a guest gallery.

An explicit owner-only action resumes an unfinished sale created by another employee, retaining the same draft, receipt, photos and pricing snapshot. The booking lock serializes transfer with sale writes; the previous employee loses editing access and the action is audited. No production draft was transferred during verification.

Folder audit status polling resumes after a temporary network error and ignores stale responses after a newer audit or folder choice. Authentication failures stop automatic polling. Reports identify duplicate candidates by their folder paths. A changing paginated directory is explicitly incomplete.

Validation prepared for publication:

- Backend regression suite passed; additional owner-resume and same-original relinking cases are included. Ruff and diff checks passed.
- All client regression scripts passed, including new recovery and three-source selection checks.
- Real-browser checks cover selecting originals from nested Disk folders, a lost acknowledgement/replay and exactly one sale and commission, plus the existing cold-offline-start and sale/photo replay scenarios.
- In the production owner session, a small existing private folder was audited: 8 of 8 originals read, 8 unique, no read failures, no exact duplicates or perceptual candidates, and AI results for all 8. This is evidence for that folder only; similarity/quality judgements require human review.
- No production sale, payment, price or source photograph was changed during the live read check. Remote CI/deployment must be checked separately.
