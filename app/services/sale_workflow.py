"""Sale workflow helpers: photographer percentage is final only after full-shoot upload."""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select

from ..models import Booking, Photo, Sale, Shooting
from .commissions import photographer_percent


async def full_shoot_state(session, booking_id: int):
    shooting = await session.scalar(
        select(Shooting).where(Shooting.booking_id == booking_id)
    )
    count = 0
    if shooting is not None:
        count = int(
            await session.scalar(
                select(func.count(Photo.id)).where(Photo.shooting_id == shooting.id)
            )
            or 0
        )
    return shooting, count


async def final_photographer_percent(session, booking_id: int):
    shooting, count = await full_shoot_state(session, booking_id)
    if shooting is None or shooting.full_upload_completed_at is None:
        return None, count
    return photographer_percent(count), count


async def finalize_photographer_commissions(session, booking: Booking):
    percent, count = await final_photographer_percent(session, booking.id)
    if percent is None or not booking.photographer_id:
        return {"finalized": False, "count": count, "percent": None, "sales": 0}
    rows = (
        await session.scalars(
            select(Sale).where(
                Sale.booking_id == booking.id,
                Sale.credited_user_id == booking.photographer_id,
                Sale.commission_role == "PHOTOGRAPHER",
            )
        )
    ).all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for sale in rows:
        amount = Decimal(str(sale.amount))
        commission = (amount * percent / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        sale.percent = float(percent)
        sale.commission = float(commission)
        sale.commission_finalized_at = now
    return {
        "finalized": True,
        "count": count,
        "percent": percent,
        "sales": len(rows),
    }
