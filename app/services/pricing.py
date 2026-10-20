"""Fixed hotel unit prices and immutable booking/sale calculation snapshots."""
import json
from decimal import Decimal

from sqlalchemy import select

from ..models import Hotel, HotelTariff, Package, Sale, SaleDraft
from .receipts import money

UNIT_PRICES = frozenset({Decimal('300.00'), Decimal('400.00'), Decimal('500.00')})
FIXED_PRICE_REQUIRED = ('В отеле не настроена фиксированная цена кадра. '
                        'Попросите владельца или администратора выбрать цену в тарифах отеля.')


def snapshot(value):
    return json.loads(value) if value else None


def _unit_quote(hotel_id, price, name, *, tariff_id=None, source):
    price = money(price)
    if not price.is_finite() or price <= 0:
        raise ValueError(FIXED_PRICE_REQUIRED)
    return {'tariffId': tariff_id, 'hotelId': hotel_id, 'name': name, 'kind': 'UNIT',
            'price': str(price), 'photoCount': None, 'source': source}


async def booking_unit_quote(session, hotel_id, package_id):
    """Resolve an explicit hotel rate before creating a new booking.

    A selected legacy Package is usable when no hotel unit tariff exists. Several
    old seeded choices are deliberately ambiguous until a supervisor sets one.
    """
    hotel = await session.get(Hotel, hotel_id)
    if not hotel or not hotel.active:
        raise ValueError('Отель недоступен.')
    if hotel.fixed_unit_tariff_id is not None:
        tariff = await session.get(HotelTariff, hotel.fixed_unit_tariff_id)
        if not tariff or not tariff.active or tariff.hotel_id != hotel_id or tariff.kind != 'UNIT':
            raise ValueError(FIXED_PRICE_REQUIRED)
    else:
        tariffs = list(await session.scalars(select(HotelTariff).where(
            HotelTariff.hotel_id == hotel_id, HotelTariff.active.is_(True), HotelTariff.kind == 'UNIT')))
        if len(tariffs) > 1:
            raise ValueError(FIXED_PRICE_REQUIRED)
        tariff = tariffs[0] if tariffs else None
    if tariff is not None:
        if money(tariff.price) not in UNIT_PRICES:
            raise ValueError(FIXED_PRICE_REQUIRED)
        return _unit_quote(hotel_id, tariff.price, tariff.name, tariff_id=tariff.id, source='hotel_fixed')
    package = await session.get(Package, package_id)
    if not package or not package.active or money(package.price_per_photo) not in UNIT_PRICES:
        raise ValueError(FIXED_PRICE_REQUIRED)
    return _unit_quote(hotel_id, package.price_per_photo, package.name, source='legacy_package_at_booking')


async def _recorded_unit_quote(session, booking):
    """Recover recorded prices without claiming that legacy packages were frozen."""
    value = getattr(booking, 'pricing_snapshot', None)
    if value:
        quote = snapshot(value)
        if quote.get('kind') == 'UNIT' and quote.get('hotelId') == booking.hotel_id:
            return quote
    for model in (Sale, SaleDraft):
        query = select(model.pricing_snapshot).where(
            model.booking_id == booking.id, model.pricing_snapshot.is_not(None))
        if model is SaleDraft:
            query = query.where(SaleDraft.status != 'CANCELLED')
        values = await session.scalars(query.order_by(model.id))
        for value in values:
            quote = snapshot(value)
            if quote.get('kind') == 'UNIT' and quote.get('hotelId') == booking.hotel_id:
                return quote
    return None


async def sale_quote(session, booking, sold, total, tariff_id=None, saved=None):
    if saved:
        quote = snapshot(saved)
        if tariff_id is not None and tariff_id != quote.get('tariffId'):
            raise ValueError('Тариф продажи уже зафиксирован. Изменять его после расчёта нельзя.')
    else:
        fixed = await _recorded_unit_quote(session, booking) if getattr(booking, 'id', None) is not None else None
        tariff = None
        if tariff_id is not None:
            if type(tariff_id) is not int or tariff_id <= 0:
                raise ValueError('Выберите действующий тариф отеля.')
            # A booking may keep the exact tariff version after its replacement.
            if not fixed or tariff_id != fixed.get('tariffId'):
                tariff = await session.get(HotelTariff, tariff_id)
                if not tariff or not tariff.active or tariff.hotel_id != booking.hotel_id:
                    raise ValueError('Тариф недоступен в этом отеле.')
        if tariff is not None and tariff.kind == 'PACKAGE':
            quote = {'tariffId': tariff.id, 'hotelId': booking.hotel_id, 'name': tariff.name,
                     'kind': tariff.kind, 'price': str(money(tariff.price)), 'photoCount': tariff.photo_count}
        else:
            if fixed is None:
                fixed = await booking_unit_quote(session, booking.hotel_id, booking.package_id)
            if tariff is not None and money(tariff.price) != money(fixed['price']):
                raise ValueError('Цена кадра зафиксирована при записи. Изменять её при продаже нельзя.')
            quote = fixed
            # Old uncounted bookings acquire a snapshot only at their first
            # actual calculation; merely reading history never changes records.
            if hasattr(booking, 'pricing_snapshot') and not booking.pricing_snapshot:
                booking.pricing_snapshot = json.dumps(fixed, ensure_ascii=False)
    price = money(quote['price'])
    if not price.is_finite() or price <= 0:
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
    """Best-effort history display; missing configuration must not hide records."""
    try:
        quote = await _recorded_unit_quote(session, booking) if getattr(booking, 'id', None) is not None else None
        if quote:
            price = money(quote['price'])
            return str(price) if price.is_finite() and price > 0 else None
        package = await session.get(Package, booking.package_id)
        if package:
            price = money(package.price_per_photo)
            return str(price) if price.is_finite() and price > 0 else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return None
