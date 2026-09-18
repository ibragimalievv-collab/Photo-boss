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
