"""Hotel price lists; immutable calculation snapshots preserve past purchases."""
import json
from decimal import Decimal

from sqlalchemy import select

from ..models import HotelTariff, Package
from .receipts import money

UNIT_PRICES = frozenset({Decimal('300.00'), Decimal('400.00'), Decimal('500.00')})

def snapshot(value):
    return json.loads(value) if value else None


async def sale_quote(session, booking, sold, total, tariff_id=None, saved=None):
    if saved:
        quote = snapshot(saved)
        if tariff_id is not None and tariff_id != quote.get('tariffId'):
            raise ValueError('Тариф продажи уже зафиксирован. Изменять его после расчёта нельзя.')
    elif tariff_id is not None:
        if type(tariff_id) is not int or tariff_id <= 0:
            raise ValueError('Выберите действующий тариф отеля.')
        tariff = await session.get(HotelTariff, tariff_id)
        if not tariff or not tariff.active or tariff.hotel_id != booking.hotel_id:
            raise ValueError('Тариф недоступен в этом отеле.')
        quote = {'tariffId': tariff.id, 'hotelId': booking.hotel_id, 'name': tariff.name,
                 'kind': tariff.kind, 'price': str(money(tariff.price)), 'photoCount': tariff.photo_count}
        if quote['kind'] == 'UNIT' and money(quote['price']) not in UNIT_PRICES:
            raise ValueError('Цена кадра должна быть 300, 400 или 500 ₽.')
    else:
        # Existing bots and older web clients use the hotel's unit rate.
        tariff = await session.scalar(select(HotelTariff).where(
            HotelTariff.hotel_id == booking.hotel_id, HotelTariff.active.is_(True),
            HotelTariff.kind == 'UNIT').order_by(HotelTariff.id.desc()).limit(1))
        if tariff:
            return await sale_quote(session, booking, sold, total, tariff.id)
        package = await session.get(Package, booking.package_id)
        if not package:
            raise ValueError('Пакет недоступен.')
        quote = {'tariffId': None, 'hotelId': booking.hotel_id, 'name': package.name,
                 'kind': 'UNIT', 'price': str(money(package.price_per_photo)), 'photoCount': None}
        if money(quote['price']) not in UNIT_PRICES:
            raise ValueError('Для новой продажи выберите тариф 300, 400 или 500 ₽ за кадр.')
    price = money(quote['price'])
    if price <= 0:
        raise ValueError('Некорректная цена тарифа.')
    if quote['kind'] == 'PACKAGE':
        count = quote['photoCount'] or total
        if sold != count:
            raise ValueError(f'В этом пакете {count} кадров. Укажите соответствующее количество.')
        amount = price
    else:
        amount = money(Decimal(sold) * price)
    line = {'type': quote['kind'], 'name': quote['name'], 'price': str(price),
            'quantity': count if quote['kind'] == 'PACKAGE' else sold,
            'amount': str(amount)}
    return quote | {'sold': sold, 'total': total, 'amount': str(amount), 'lines': [line]}


async def unit_price(session, booking):
    quote = await sale_quote(session, booking, 1, 1)
    return quote['price']
