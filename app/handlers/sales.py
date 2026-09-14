from decimal import Decimal, ROUND_HALF_UP

from aiogram import F, Router
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from ..access import StaffFilter
from ..config import config, number
from ..db import Session
from ..models import Booking, Compensation, Package, Sale, User
from ..services.core import audit, get_user, roles_of, setting

r = Router()
r.message.filter(StaffFilter('PHOTOGRAPHER', 'MANAGER'), F.text)


class S(StatesGroup):
    booking = State()
    credited = State()
    role = State()
    photos = State()


@r.message(F.text == '🧾 Продажа')
async def begin(m, state):
    async with Session() as session:
        bookings = (await session.execute(select(Booking).order_by(Booking.id.desc()).limit(20))).scalars().all()
    if not bookings:
        await state.clear()
        return await m.answer('Записей для продажи пока нет.')
    await state.clear()
    await state.set_state(S.booking)
    await m.answer('Введите ID записи:\n' + ', '.join(str(booking.id) for booking in bookings) + '\nОтмена: /cancel')


@r.message(S.booking)
async def b(m, state):
    try:
        bid = int(m.text)
        if not 0 < bid <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer('Нужен положительный числовой ID записи.')
    async with Session() as session:
        booking = await session.get(Booking, bid)
        if booking is None:
            return await m.answer('Запись не найдена.')
        photographer = await session.get(User, booking.photographer_id) if booking.photographer_id else None
    await state.update_data(booking=bid)
    await state.set_state(S.credited)
    hint = f'\nФотограф записи: {photographer.name}, Telegram ID {photographer.tg_id}.' if photographer else ''
    await m.answer('Введите Telegram ID сотрудника, кому засчитать продажу:' + hint)


@r.message(S.credited)
async def cr(m, state):
    try:
        tg_id = int(m.text)
        if not 0 < tg_id < 2**52:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer('Нужен положительный Telegram ID.')
    async with Session() as session:
        credited = await get_user(session, tg_id)
        roles = await roles_of(session, credited)
    commission_roles = roles & {'MANAGER', 'PHOTOGRAPHER'}
    if not commission_roles:
        return await m.answer('Нужен активный сотрудник с ролью MANAGER или PHOTOGRAPHER.')
    await state.update_data(credited=credited.id)
    await state.set_state(S.role)
    await m.answer('Роль для комиссии: ' + ' или '.join(sorted(commission_roles)))


@r.message(S.role)
async def rr(m, state):
    role = (m.text or '').strip().upper()
    data = await state.get_data()
    async with Session() as session:
        credited = await session.get(User, data['credited'])
        roles = await roles_of(session, credited)
    if role not in {'MANAGER', 'PHOTOGRAPHER'} or role not in roles:
        return await m.answer('Выберите роль, назначенную этому сотруднику: MANAGER или PHOTOGRAPHER.')
    await state.update_data(role=role)
    await state.set_state(S.photos)
    await m.answer('Сколько фото продано?')


@r.message(S.photos)
async def save(m, state):
    try:
        count = int(m.text)
        if not 0 < count <= 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer('Введите положительное целое число фото.')
    data = await state.get_data()
    async with Session() as session:
        creator = await get_user(session, m.from_user.id)
        creator_roles = await roles_of(session, creator)
        if not creator_roles & {'OWNER', 'ADMIN', 'MANAGER', 'PHOTOGRAPHER'}:
            await state.clear()
            return await m.answer('Нет доступа.')
        booking = await session.get(Booking, data.get('booking'))
        credited = await session.get(User, data.get('credited'))
        role = data.get('role')
        if booking is None or role not in {'MANAGER', 'PHOTOGRAPHER'} or role not in await roles_of(session, credited):
            await state.clear()
            return await m.answer('Запись или права сотрудника изменились. Начните продажу заново.')
        package = await session.get(Package, booking.package_id)
        if package is None:
            return await m.answer('У записи не найден пакет. Обратитесь к администратору.')
        compensations = (await session.execute(select(Compensation).where(Compensation.user_id == credited.id, Compensation.role == role))).scalars().all()
        if len(compensations) > 1:
            return await m.answer('Найдено несколько настроек комиссии сотрудника. Обратитесь к администратору.')
        key = f'{role}_PERCENT'
        default = config.manager_percent if role == 'MANAGER' else config.photographer_percent
        percent_value = compensations[0].sales_percent if compensations else await setting(session, key, default)
        try:
            price = Decimal(str(number(package.price_per_photo, 'Цена', minimum=0.01)))
            percent = Decimal(str(number(percent_value, 'Комиссия', maximum=100)))
        except ValueError:
            return await m.answer('Некорректная цена или комиссия. Обратитесь к администратору.')
        amount = (count * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        commission = (amount * percent / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        sale = Sale(booking_id=booking.id, created_by_id=creator.id, credited_user_id=credited.id, commission_role=role, sold_photos=count, amount=float(amount), percent=float(percent), commission=float(commission))
        session.add(sale)
        await session.flush()
        await audit(session, creator, 'sale_created', 'sale', sale.id, f'credited={credited.id};role={role}')
        await session.commit()
    await state.clear()
    await m.answer(f'Продажа #{sale.id} сохранена: {amount:.2f} ₽; комиссия {commission:.2f} ₽.\nЗасчитано: {credited.name} (Telegram ID {credited.tg_id}).')
