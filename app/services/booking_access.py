"""Current database grants define booking visibility across bot and Mini App."""
from sqlalchemy import false, or_, select, true

from ..miniapp_security import booking_assignment_columns
from ..models import Booking, HotelEmployee


def booking_scope(user_id, roles, *, photos=False):
    roles = set(roles)
    if 'OWNER' in roles:
        return true()
    if 'ADMIN' in roles:
        return Booking.hotel_id.in_(select(HotelEmployee.hotel_id).where(
            HotelEmployee.user_id == user_id))
    columns = ('photographer_id',) if photos and 'PHOTOGRAPHER' in roles else (
        () if photos else booking_assignment_columns(roles))
    return or_(false(), *(getattr(Booking, column) == user_id for column in columns))


async def can_access_booking(session, user_id, roles, booking, *, photos=False):
    if booking is None:
        return False
    return bool(await session.scalar(select(Booking.id).where(
        Booking.id == booking.id, booking_scope(user_id, roles, photos=photos))))


def booking_scope_sql(user_id, roles, alias='b', *, photos=False):
    """Only internal aliases and server-loaded roles may be supplied."""
    roles = set(roles)
    if 'OWNER' in roles:
        return '1=1'
    if 'ADMIN' in roles:
        return (f'{alias}.hotel_id IN (SELECT hotel_id FROM hotel_employees '
                'WHERE user_id=:uid)')
    columns = ('photographer_id',) if photos and 'PHOTOGRAPHER' in roles else (
        () if photos else booking_assignment_columns(roles))
    return ' OR '.join(f'{alias}.{column}=:uid' for column in columns) or '1=0'
