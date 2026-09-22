from decimal import Decimal

PHOTO_TIER_THRESHOLD = 150
PHOTO_PERCENT_STANDARD = Decimal(10)
PHOTO_PERCENT_HIGH = Decimal(15)


def photographer_percent(shoot_photo_count: int) -> Decimal:
    """Use all recorded photos in one shoot, regardless of how many are sold."""
    return (
        PHOTO_PERCENT_HIGH
        if shoot_photo_count >= PHOTO_TIER_THRESHOLD
        else PHOTO_PERCENT_STANDARD
    )


MANAGER_PERCENT = Decimal(15)


def hotel_photo_price(name: str) -> Decimal:
    return {"гранд": Decimal(500), "эллада": Decimal(300)}.get(
        name.strip().casefold(), Decimal(400)
    )


def photographer_bonus(amount) -> Decimal:
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Некорректная сумма продажи.")
    return Decimal(0) if amount < 21000 else Decimal(1000) + ((amount - 21000) // 5000) * 500


def manager_booking_bonus(count: int) -> Decimal:
    return Decimal(0) if count < 5 else Decimal(500 + (count - 5) * 100)


async def booking_photo_price(session, booking):
    from ..models import Hotel
    hotel = await session.get(Hotel, booking.hotel_id)
    if hotel is None:
        raise ValueError("Отель не найден.")
    return hotel_photo_price(hotel.name)
