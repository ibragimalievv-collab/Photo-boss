"""Apply the owner's 21 October price instruction once, retaining later edits."""
import json
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Hotel, HotelAlbumTariff, HotelTariff, Setting, User, UserRole
from .operations_guard import sqlite_operation_barrier
from .services.core import audit
from .services.receipts import money

REQUEST_KEY = 'hotel-prices:owner-20261021-v1'
UNIT_PRICES_BY_NAME = {
    'Фиолето': 400, 'Город мира': 400, 'Бетон': 400, 'Славянка': 400,
    'Эллада': 300, 'Гранд': 500, 'Фюнф': 500, 'Дусит': 500,
}
ALBUM_PRICES = {'Маленький альбом': 10000, 'Средний альбом': 15000, 'Большой альбом': 20000}
log = logging.getLogger(__name__)


def normalized_name(name):
    name = ' '.join(name.casefold().split())
    return 'фиолето' if name == 'фиолетта' else name


async def apply_owner_hotel_prices(engine, admin_ids):
    """No hotel creation, no historical quote edits, no repeated price reset."""
    async with sqlite_operation_barrier(engine), AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            if engine.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 7620182632542})
            recorded = await session.get(Setting, REQUEST_KEY, with_for_update=True)
            if recorded:
                result = json.loads(recorded.value) | {'replayed': True}
            else:
                owner = await session.scalar(select(User).join(UserRole, UserRole.user_id == User.id).where(
                    User.active.is_(True), UserRole.role == 'OWNER', User.tg_id.in_(list(admin_ids))).order_by(User.id).limit(1))
                if owner is None:
                    raise RuntimeError('Configured active owner required for hotel price instruction')
                hotels = list(await session.scalars(select(Hotel).where(Hotel.active.is_(True)).order_by(Hotel.id).with_for_update()))
                applied, missing, ambiguous = [], [], []
                for name, price in UNIT_PRICES_BY_NAME.items():
                    matches = [hotel for hotel in hotels if normalized_name(hotel.name) == normalized_name(name)]
                    if not matches:
                        missing.append(name)
                        continue
                    if len(matches) != 1:
                        ambiguous.append(name)
                        continue
                    hotel = matches[0]
                    rates = list(await session.scalars(select(HotelTariff).where(
                        HotelTariff.hotel_id == hotel.id, HotelTariff.kind == 'UNIT', HotelTariff.active.is_(True))))
                    chosen = next((rate for rate in rates if money(rate.price) == money(price)), None)
                    if chosen is None:
                        chosen = HotelTariff(hotel_id=hotel.id, name='Цена кадра', kind='UNIT', price=price)
                        session.add(chosen)
                        await session.flush()
                    for rate in rates:
                        if rate is not chosen:
                            rate.active = False
                    hotel.fixed_unit_tariff_id = chosen.id
                    await audit(session, owner, 'hotel_tariff_owner_instruction', 'hotel', hotel.id,
                                json.dumps({'requestId': REQUEST_KEY, 'unitPrice': price, 'tariffId': chosen.id}, ensure_ascii=False))
                    applied.append({'hotelId': hotel.id, 'name': hotel.name, 'price': str(money(price))})
                albums = 0
                for hotel in hotels:
                    for name, price in ALBUM_PRICES.items():
                        previous = list(await session.scalars(select(HotelAlbumTariff).where(
                            HotelAlbumTariff.hotel_id == hotel.id, HotelAlbumTariff.name == name,
                            HotelAlbumTariff.active.is_(True))))
                        # This exact instruction owns the initial catalogue;
                        # subsequent supervisor edits survive every restart.
                        for old in previous:
                            old.active = False
                        album = HotelAlbumTariff(hotel_id=hotel.id, name=name, price=price)
                        session.add(album)
                        await session.flush()
                        await audit(session, owner, 'hotel_album_owner_instruction', 'hotel_album_tariff', album.id,
                                    json.dumps({'requestId': REQUEST_KEY, 'hotelId': hotel.id, 'price': price}, ensure_ascii=False))
                        albums += 1
                result = {'requestId': REQUEST_KEY, 'applied': applied, 'missing': missing,
                          'ambiguous': ambiguous, 'albumsCreated': albums, 'replayed': False}
                session.add(Setting(key=REQUEST_KEY, value=json.dumps(result, ensure_ascii=False)))
    log.info('Owner hotel price instruction verified: %s', json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
