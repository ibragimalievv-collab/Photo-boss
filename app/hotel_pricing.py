"""Supervisors edit hotel price lists by replacing versions, never past sales."""
from decimal import InvalidOperation

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError
from .models import HotelTariff, User
from .services.core import audit, roles_of
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
        if (type(body['hotelId']) is not int or not isinstance(body['name'], str) or not 2 <= len(body['name'].strip()) <= 150
                or body['kind'] not in {'UNIT', 'PACKAGE'} or not 0 < price <= 10000000
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
            await session.get(Hotel, body['hotelId'], with_for_update=True)
            old = await session.get(HotelTariff, body['replaces'], with_for_update=True) if body['replaces'] else None
            if body['replaces'] and (not old or old.hotel_id != body['hotelId'] or not old.active):
                raise AccessError('Тариф уже изменён. Обновите данные.', 409)
            if old:
                old.active = False
            if body['kind'] == 'UNIT':
                for previous in await session.scalars(select(HotelTariff).where(
                        HotelTariff.hotel_id == body['hotelId'], HotelTariff.kind == 'UNIT', HotelTariff.active.is_(True))):
                    previous.active = False
            tariff = HotelTariff(hotel_id=body['hotelId'], name=body['name'].strip(),
                kind=body['kind'], price=price, photo_count=body['photoCount'])
            session.add(tariff)
            await session.flush()
            await audit(session, actor, 'hotel_tariff_saved', 'hotel_tariff', tariff.id,
                        f'hotel={tariff.hotel_id};replaces={body["replaces"]};price={price};kind={tariff.kind}')
            await session.commit()
        return web.json_response({'ok': True, 'id': tariff.id})
