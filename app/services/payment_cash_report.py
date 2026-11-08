"""One cash basis: dated immutable events, with unrepresented legacy receipts.

The legacy fallback is excluded by ANY event for a receipt, across all dates.
Consequently a refund today cannot erase yesterday's income, and selecting only
today cannot count both Receipt NET and a negative ledger event.
"""
from datetime import UTC

from ..refund_schema import enabled


async def cash_rows(api, conn, lower, upper, *, booking_clause='', uid=None):
    # Existing Receipt.reviewed_at is naive UTC; the new canonical event clock
    # is TIMESTAMPTZ. asyncpg requires the corresponding bound Python types.
    lo = lower.replace(tzinfo=UTC) if lower.tzinfo is None else lower.astimezone(UTC)
    hi = upper.replace(tzinfo=UTC) if upper.tzinfo is None else upper.astimezone(UTC)
    params = {'lo': lo.replace(tzinfo=None), 'hi': hi.replace(tzinfo=None), 'uid': uid}
    clause = ' AND (' + booking_clause + ')' if booking_clause else ''
    if not enabled():
        rows = await api.rows(conn, """SELECT r.id AS receipt_id,r.booking_id,b.hotel_id,
            r.verified_amount,r.reviewed_at AS occurred_at,'LEGACY_RECEIPT' AS date_source
            FROM receipts r JOIN bookings b ON b.id=r.booking_id
            WHERE r.status='APPROVED' AND r.reviewed_at>=:lo AND r.reviewed_at<:hi""" + clause,
            **params)
        from .insights import cents
        return [dict(row, amount_kopecks=cents(row['verified_amount'])) for row in rows]
    events = await api.rows(conn, """SELECT e.receipt_id,e.booking_id,b.hotel_id,
        e.amount_kopecks,e.occurred_at,e.date_source FROM payment_cash_events e
        JOIN bookings b ON b.id=e.booking_id
        WHERE e.occurred_at>=:lo AND e.occurred_at<:hi""" + clause,
        **dict(params, lo=lo, hi=hi))
    legacy = await api.rows(conn, """SELECT r.id AS receipt_id,r.booking_id,b.hotel_id,
        r.verified_amount,r.reviewed_at AS occurred_at,'LEGACY_RECEIPT' AS date_source
        FROM receipts r JOIN bookings b ON b.id=r.booking_id
        WHERE r.status='APPROVED' AND r.reviewed_at>=:lo AND r.reviewed_at<:hi
        AND NOT EXISTS (SELECT 1 FROM payment_cash_events ce WHERE ce.receipt_id=r.id)""" + clause,
        **params)
    from .insights import cents
    return list(events) + [dict(row, amount_kopecks=cents(row['verified_amount'])) for row in legacy]
