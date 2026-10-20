"""Supervisors edit hotel price lists by replacing versions, never past sales."""
from decimal import InvalidOperation

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import HotelTariff, User
from .services.core import audit, roles_of
from .services.pricing import UNIT_PRICES
from .services.receipts import money
from .work_breaks import hotel_access


class HotelPricing:
    def __init__(self, api):
        self.api = api

    async def save(self, request):
        body, a = await self.api.body(request), request['miniapp_actor']
        if set(body) != {'hotelId', 'name', 'kind', 'price', 'photoCount', 'replaces'}:
            raise AccessError('Проверьте поля тарифа.', 400)
        try:
            price = money(body['price'])
        except (ValueError, TypeError, InvalidOperation):
            raise AccessError('Укажите корректную цену.', 400) from None
        if not price.is_finite():
            raise AccessError('Укажите конечную положительную цену.', 400)
        if (type(body['hotelId']) is not int or not isinstance(body['name'], str) or not 2 <= len(body['name'].strip()) <= 150
                or not isinstance(body['kind'], str) or body['kind'] not in {'UNIT', 'PACKAGE'} or not 0 < price <= 10000000
                or (body['kind'] == 'UNIT' and price not in UNIT_PRICES)
                or (body['photoCount'] is not None and (type(body['photoCount']) is not int or not 1 <= body['photoCount'] <= 10000))
                or (body['kind'] == 'UNIT' and body['photoCount'] is not None)
                or (body['replaces'] is not None and type(body['replaces']) is not int)):
            raise AccessError('Проверьте название, тип, цену и число кадров тарифа.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor = await session.get(User, a['id'], with_for_update=True)
            roles = await roles_of(session, actor) if actor and actor.active else set()
            await hotel_access(session, a['id'], roles, body['hotelId'], manage=True)
            # Serialize unit-rate replacement across different supervisors.
            from .models import Hotel
            hotel = await session.get(Hotel, body['hotelId'], with_for_update=True)
            old = await session.get(HotelTariff, body['replaces'], with_for_update=True) if body['replaces'] else None
            if body['replaces'] and (not old or old.hotel_id != body['hotelId'] or not old.active):
                raise AccessError('Тариф уже изменён. Обновите данные.', 409)
            if body['kind'] == 'UNIT':
                # Legacy releases seeded three simultaneous choices. A
                # supervisor's explicit save replaces that ambiguity with one
                # fixed rate. Booking and sale snapshots remain untouched.
                units = await session.scalars(select(HotelTariff).where(
                    HotelTariff.hotel_id == body['hotelId'], HotelTariff.kind == 'UNIT',
                    HotelTariff.active.is_(True)))
                for unit in units:
                    unit.active = False
            if old:
                old.active = False
            tariff = HotelTariff(hotel_id=body['hotelId'], name=body['name'].strip(),
                kind=body['kind'], price=price, photo_count=body['photoCount'])
            session.add(tariff)
            await session.flush()
            if body['kind'] == 'UNIT':
                hotel.fixed_unit_tariff_id = tariff.id
            await audit(session, actor, 'hotel_tariff_saved', 'hotel_tariff', tariff.id,
                        f'hotel={tariff.hotel_id};replaces={body["replaces"]};price={price};kind={tariff.kind}')
            await session.commit()
        return web.json_response({'ok': True, 'id': tariff.id,
            'fixedUnitTariffId': hotel.fixed_unit_tariff_id,
            'unitPrice': str(price) if tariff.kind == 'UNIT' else None})
