import asyncio
import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_team import Request
from test_workflow_services import booking_data, fixture

from app.hotel_price_defaults import (
    ALBUM_PRICES,
    UNIT_PRICES_BY_NAME,
    apply_owner_hotel_prices,
)
from app.hotel_pricing import HotelPricing
from app.miniapp_security import AccessError
from app.models import (
    Booking,
    Hotel,
    HotelAlbumTariff,
    HotelEmployee,
    HotelTariff,
    SaleDraft,
    User,
    UserRole,
)
from app.services.bookings import create_booking_record
from app.services.pricing import booking_unit_quote, sale_quote


def test_owner_instruction_sets_exact_hotels_once_keeps_movenpick_and_old_quotes():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                session.add(UserRole(user_id=1, role='OWNER'))
                (await session.get(Hotel, 1)).name = 'Дусит'
                manager = await session.get(User, 1)
                older = await create_booking_record(session, manager, booking_data())
                for name in list(UNIT_PRICES_BY_NAME)[0:-1] + ['Мовенпик']:
                    session.add(Hotel(name=name))
                await session.flush()
                for hotel in await session.scalars(select(Hotel)):
                    for price in (300, 400, 500):
                        session.add(HotelTariff(hotel_id=hotel.id, name=f'Поштучно — {price} ₽/кадр', kind='UNIT', price=price))
                await session.commit()
                old_id = older.id
            result = await apply_owner_hotel_prices(engine, {1})
            assert not result['replayed'] and not result['missing'] and not result['ambiguous']
            assert len(result['applied']) == 8 and result['albumsCreated'] == 27
            async with factory() as session:
                hotel_count = await session.scalar(select(func.count(Hotel.id)))
                assert hotel_count == 9
                for hotel in await session.scalars(select(Hotel)):
                    quote = await booking_unit_quote(session, hotel.id, 1)
                    assert Decimal(quote['price']) == UNIT_PRICES_BY_NAME.get(hotel.name, 400)
                    if hotel.name == 'Мовенпик':
                        assert hotel.fixed_unit_tariff_id is None
                        assert quote['source'] == 'legacy_package_at_booking'
                    albums = list(await session.scalars(select(HotelAlbumTariff).where(HotelAlbumTariff.hotel_id == hotel.id)))
                    assert {a.name: int(a.price) for a in albums} == ALBUM_PRICES
                old = await session.get(Booking, old_id)
                assert (await sale_quote(session, old, 10, 150))['amount'] == '4000.00'
                current = await session.get(HotelTariff, (await session.get(Hotel, 1)).fixed_unit_tariff_id)
                current.price = 300  # A later explicit administrative edit must survive restart.
                album = await session.scalar(select(HotelAlbumTariff).limit(1))
                album.price = 11000
                await session.commit()
            assert (await apply_owner_hotel_prices(engine, set()))['replayed']
            async with factory() as session:
                assert (await booking_unit_quote(session, 1, 1))['price'] == '300.00'
                assert await session.scalar(select(HotelAlbumTariff.price).order_by(HotelAlbumTariff.id).limit(1)) == Decimal(11000)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_price_instruction_requires_owner_and_refuses_duplicate_name_matches():
    async def run():
        engine, factory = await fixture()
        try:
            with pytest.raises(RuntimeError, match='owner'):
                await apply_owner_hotel_prices(engine, {1})
            async with factory() as session:
                session.add_all([UserRole(user_id=1, role='OWNER'), Hotel(name='Дусит'), Hotel(name=' дусит ')])
                await session.commit()
            result = await apply_owner_hotel_prices(engine, {1})
            assert result['ambiguous'] == ['Дусит']
            assert result['applied'] == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_legacy_generated_choices_use_existing_selected_package_custom_choices_still_fail():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                for price in (300, 400, 500):
                    session.add(HotelTariff(hotel_id=1, name=f'Поштучно — {price} ₽/кадр', kind='UNIT', price=price))
                await session.flush()
                assert (await booking_unit_quote(session, 1, 1))['price'] == '400.00'
                custom = await session.scalar(select(HotelTariff).limit(1))
                custom.name = 'Ручная настройка'
                with pytest.raises(ValueError, match='фиксированная цена'):
                    await booking_unit_quote(session, 1, 1)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_album_price_is_separate_snapshot_and_edit_permissions_are_server_checked():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def run():
        engine, factory = await fixture()
        api = SimpleNamespace(engine=engine, body=AsyncMock(side_effect=lambda request: request.body))
        body = {'hotelId': 1, 'name': 'Маленький альбом', 'kind': 'ALBUM', 'price': '10000', 'photoCount': None, 'replaces': None}
        try:
            with pytest.raises(AccessError):
                await HotelPricing(api).save(Request({'id': 1, 'roles': ['OWNER']}, body))
            async with factory() as session:
                session.add_all([UserRole(user_id=1, role='ADMIN'), HotelEmployee(user_id=1, hotel_id=1), Hotel(id=2, name='Чужой')])
                await session.commit()
            with pytest.raises(AccessError):
                await HotelPricing(api).save(Request({'id': 1, 'roles': ['ADMIN']}, body | {'hotelId': 2}))
            response = json.loads((await HotelPricing(api).save(Request({'id': 1}, body))).text)
            aid = response['id']
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                quote = await sale_quote(session, booking, 10, 150, album_id=aid, album_quantity=2)
                assert quote['amount'] == '24000.00'
                assert quote['sold'] == 10 and quote['lines'][0]['amount'] == '4000.00'
                assert quote['lines'][1] == {'type': 'ALBUM', 'albumId': aid, 'name': 'Маленький альбом', 'price': '10000.00', 'quantity': 2, 'amount': '20000.00'}
                (await session.get(HotelAlbumTariff, aid)).price = 12000
                (await session.get(HotelAlbumTariff, aid)).active = False
                saved = json.dumps(quote)
                assert (await sale_quote(session, booking, 10, 150, saved=saved))['amount'] == '24000.00'
                with pytest.raises(ValueError, match='зафиксированы'):
                    await sale_quote(session, booking, 10, 150, saved=saved, album_quantity=1)
                with pytest.raises(ValueError, match='недоступен'):
                    await sale_quote(session, booking, 10, 150, album_id=aid)
                base = await sale_quote(session, booking, 10, 150)
                with pytest.raises(ValueError, match='зафиксированы'):
                    await sale_quote(session, booking, 10, 150, saved=json.dumps(base), album_id=aid)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_recovered_unit_price_does_not_inherit_an_older_sale_album_or_discount():
    async def run():
        engine, factory = await fixture()
        try:
            async with factory() as session:
                actor = await session.get(User, 1)
                booking = await create_booking_record(session, actor, booking_data())
                booking.pricing_snapshot = None
                session.add(SaleDraft(booking_id=booking.id, created_by_id=actor.id, status='AWAITING_SELECTED',
                    declared_photo_count=150, sold_photos=10,
                    pricing_snapshot=json.dumps({'kind': 'UNIT', 'hotelId': 1, 'price': '400.00',
                        'tariffId': None, 'name': 'Цена записи', 'photoCount': None, 'discountId': 999,
                        'lines': [{'type': 'ALBUM', 'albumId': 999, 'quantity': 2, 'price': '10000.00', 'amount': '20000.00'}]})))
                await session.flush()
                quote = await sale_quote(session, booking, 10, 150)
                assert quote['amount'] == '4000.00'
                assert 'discountId' not in quote and len(quote['lines']) == 1
                assert json.loads(booking.pricing_snapshot)['price'] == '400.00'
        finally:
            await engine.dispose()
    asyncio.run(run())
