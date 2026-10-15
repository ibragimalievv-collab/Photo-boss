from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from ..models import Booking, PayrollEntry, Sale

PHOTO_TIER_THRESHOLD = 150
PHOTO_PERCENT_STANDARD = Decimal(10)
PHOTO_PERCENT_HIGH = Decimal(15)
PHOTO_BONUS_THRESHOLD = Decimal(21000)
PHOTO_BONUS_STEP = Decimal(5000)
PHOTO_BONUS_INITIAL = Decimal(1000)
PHOTO_BONUS_INCREMENT = Decimal(500)
PHOTO_BONUS_KIND = "Премия фотографа"


def photographer_percent(shoot_photo_count: int) -> Decimal:
    """Use all recorded photos in one shoot, regardless of how many are sold."""
    return (
        PHOTO_PERCENT_HIGH
        if shoot_photo_count >= PHOTO_TIER_THRESHOLD
        else PHOTO_PERCENT_STANDARD
    )


def photographer_bonus(sales_amount) -> Decimal:
    """A shoot earns 1,000 at 21,000, then 500 per complete extra 5,000."""
    try:
        amount = Decimal(str(sales_amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Некорректная сумма продаж для премии.") from exc
    if not amount.is_finite():
        raise ValueError("Некорректная сумма продаж для премии.")
    if amount < PHOTO_BONUS_THRESHOLD:
        return Decimal("0.00")
    steps = (amount - PHOTO_BONUS_THRESHOLD) // PHOTO_BONUS_STEP
    return (PHOTO_BONUS_INITIAL + steps * PHOTO_BONUS_INCREMENT).quantize(
        Decimal("0.01")
    )


async def reconcile_photographer_bonus(session, booking: Booking):
    """Recompute one accrual per shoot/photographer, including split sales.

    The booking lock serializes creation with the sale service. A unique source
    key also protects the accrual from accidental duplicate inserts. Replaying
    this helper changes the existing accrual rather than issuing another bonus;
    correcting/removing sales reduces it without touching actual cash payouts.
    """
    await session.execute(
        select(Booking.id).where(Booking.id == booking.id).with_for_update()
    )
    await session.flush()
    rows = await session.execute(
        select(Sale.credited_user_id, Sale.amount).where(
            Sale.booking_id == booking.id,
            Sale.commission_role == "PHOTOGRAPHER",
        )
    )
    totals = {}
    for user_id, amount in rows:
        value = Decimal(str(amount))
        if not value.is_finite():
            raise ValueError("Некорректная сумма продаж для премии.")
        totals[user_id] = totals.get(user_id, Decimal(0)) + value

    prefix = f"photographer-bonus:booking:{booking.id}:user:"
    existing = list(await session.scalars(
        select(PayrollEntry).where(PayrollEntry.source_key.startswith(prefix))
    ))
    entries = {entry.user_id: entry for entry in existing}
    for user_id in totals.keys() | entries.keys():
        total = totals.get(user_id, Decimal(0))
        amount = photographer_bonus(total)
        entry = entries.get(user_id)
        if entry is None:
            if not amount:
                continue
            entry = PayrollEntry(
                user_id=user_id,
                kind=PHOTO_BONUS_KIND,
                amount=float(amount),
                period=datetime.now(UTC).date().isoformat(),
                source_key=f"{prefix}{user_id}",
            )
            session.add(entry)
        entry.amount = float(amount)
        entry.note = f"booking={booking.id};photographer={user_id};sales={total:.2f}"
    await session.flush()
    return {user_id: photographer_bonus(total) for user_id, total in totals.items()}
