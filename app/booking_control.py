"""On-shift booking reminders and hotel staffing advice from recorded activity."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .miniapp_security import AccessError, require_owner
from .models import (
    Booking,
    Hotel,
    HotelEmployee,
    Notification,
    Setting,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)
from .services.development_ai import STR, obj, structured


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def preferences(session):
    row = await session.get(Setting, 'booking-control:settings')
    try:
        data = json.loads(row.value) if row else {}
    except (ValueError, TypeError):
        data = {}
    interval = data.get('idleMinutes', 60)
    return {'enabled': data.get('enabled', True) is True,
            'idleMinutes': interval if type(interval) is int and 15 <= interval <= 240 else 60}


async def activity(session, *, now=None, hotel_ids=None):
    now = utc(now or datetime.now(UTC))
    local = now.astimezone(ZoneInfo(config.training_timezone))
    start = datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo).astimezone(UTC).replace(tzinfo=None)
    moment = now.replace(tzinfo=None)
    hotels = list(await session.scalars(select(Hotel).where(Hotel.active.is_(True)).order_by(Hotel.name)))
    if hotel_ids is not None:
        hotels = [h for h in hotels if h.id in hotel_ids]
    allowed = {h.id for h in hotels}
    from .services.discipline import operational_reset_at
    reset_at = await operational_reset_at(session)
    shifts = (await session.execute(select(Shift, User, ShiftCheckIn.started_at)
        .join(User, User.id == Shift.user_id).join(ShiftCheckIn, ShiftCheckIn.user_id == User.id)
        .where(Shift.hotel_id.in_(allowed), Shift.status != 'CANCELLED', User.active.is_(True),
               Shift.start_at <= moment, Shift.end_at > moment,
               ShiftCheckIn.shift_date == local.date(), ShiftCheckIn.status == 'STARTED',
               ~select(ShiftCheckOut.id).where(ShiftCheckOut.user_id == User.id,
                    ShiftCheckOut.shift_date == local.date(), ShiftCheckOut.status == 'FINISHED').exists(),
               *([ShiftCheckIn.started_at >= reset_at.replace(tzinfo=None)] if reset_at else [])))).all()
    roles = {}
    for uid, role in (await session.execute(select(UserRole.user_id, UserRole.role))).all():
        roles.setdefault(uid, set()).add(role)
    bookings = list(await session.scalars(select(Booking).where(
        Booking.hotel_id.in_(allowed), Booking.created_at >= start, Booking.created_at <= moment,
        Booking.status.notin_(['CANCELLED', 'REJECTED']))))
    upcoming = dict((await session.execute(select(Booking.hotel_id, func.count()).where(
        Booking.hotel_id.in_(allowed), Booking.shoot_date == local.date(), Booking.shoot_time >= local.time().replace(tzinfo=None),
        Booking.status.notin_(['CANCELLED', 'REJECTED', 'COMPLETED', 'SOLD'])).group_by(Booking.hotel_id))).all())
    stats, managers = [], []
    for h in hotels:
        team = [(sh, user, since) for sh, user, since in shifts if sh.hotel_id == h.id]
        manager_ids = {u.id for _, u, _ in team if 'MANAGER' in roles.get(u.id, set())}
        photo_ids = {u.id for _, u, _ in team if 'PHOTOGRAPHER' in roles.get(u.id, set())}
        made = [b for b in bookings if b.hotel_id == h.id]
        manager_names = sorted({u.id: u.name for _, u, _ in team if u.id in manager_ids}.items())
        stats.append({'id': h.id, 'name': h.name, 'bookings': len(made), 'managers': len(manager_ids),
                      'managerNames': [name for _, name in manager_names],
                      'photographers': len(photo_ids), 'upcoming': upcoming.get(h.id, 0)})
        for sh, u, since in team:
            if u.id not in manager_ids:
                continue
            begin = max(utc(sh.start_at), utc(since or sh.start_at))
            own = [b for b in made if b.manager_id == u.id and utc(b.created_at) >= begin]
            latest = max(own, key=lambda b: b.created_at) if own else None
            anchor = max(begin, utc(latest.created_at)) if latest else begin
            managers.append({'user': u, 'shift': sh, 'hotelId': h.id, 'hotel': h.name, 'bookings': len(own),
                             'managerNames': [name for _, name in manager_names],
                             'idleMinutes': max(0, int((now-anchor).total_seconds()//60)),
                             'lastBookingId': latest.id if latest else 0})
    targets = sorted([h for h in stats if h['photographers']],
                     key=lambda h: (h['bookings']/max(1, h['managers']), h['managers'], h['name']))
    choices = []
    for target in targets:
        sources = [h for h in stats if h['id'] != target['id'] and h['managers'] >= 2
                   and h['bookings']/h['managers'] >= target['bookings']/max(target['managers'], 1)+2]
        for source in sorted(sources, key=lambda h: h['bookings']/h['managers'], reverse=True):
            choices.append({'sourceId': source['id'], 'targetId': target['id'],
                'text': f"Рассмотрите перевод одного менеджера из «{source['name']}» в «{target['name']}»: "
                        f"создано записей сегодня {source['bookings']} и {target['bookings']}, "
                        f"На смене в «{source['name']}»: {roster(source)}; в «{target['name']}»: {roster(target)}. "
                        'Перед переводом проверьте свободное время фотографа и поток гостей.'})
    advice = choices[0]['text'] if choices else (
        f"Проверьте приглашения гостей в «{targets[0]['name']}»: создано записей сегодня {targets[0]['bookings']}, "
        f"на смене: {roster(targets[0])}. Данных для обоснованного перевода менеджера между отелями недостаточно."
        if targets else 'Нет подтверждённых фотографов на смене. Сначала проверьте отметки смен и доступность фотографов.')
    return {'date': str(local.date()), 'hotels': stats, 'zeroBookingHotels': [h for h in stats if not h['bookings']],
            'managers': managers, 'choices': choices,
            'advice': advice, 'limitations': 'Совет основан на созданных записях и подтверждённых сменах. '
            'Загрузка отеля, свободные интервалы и поток гостей автоматически не измеряются. Перевод подтверждает администратор.'}


def roster(hotel):
    return ', '.join(hotel['managerNames']) or 'нет менеджеров с подтверждённой сменой'


def activity_fingerprint(data):
    return hashlib.sha256(json.dumps({'date': data['date'], 'hotels': data['hotels']},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def advisor(session, actor_id, data, now):
    key = f'booking-control:advice:{actor_id}'
    row = await session.get(Setting, key)
    if row:
        try:
            cached = json.loads(row.value)
            if isinstance(cached, dict) and cached.get('fingerprint') == activity_fingerprint(data) and utc(now)-utc(datetime.fromisoformat(cached['at'])) < timedelta(hours=1):
                return cached
        except (ValueError, KeyError, TypeError):
            pass
    advice = {'at': utc(now).isoformat(), 'source': 'statistics', 'text': data['advice'],
              'hotelIds': sorted(h['id'] for h in data['hotels']), 'fingerprint': activity_fingerprint(data)}
    # The model chooses only among validated transfers; it cannot invent hotels,
    # move employees, or turn unknown guest traffic into a measured fact.
    if config.openai_api_key and data['choices']:
        schema = obj({'choice': {'type': 'integer', 'enum': list(range(len(data['choices'])))}, 'reason': STR})
        response = await structured('Ты помощник администратора Photo Boss. Выбери один вариант из choices. '
            'Пиши по-русски. В reason кратко объясни принцип выбора без чисел и без выдуманных данных о гостях. '
            'Это совет, а не приказ; не оценивай личные качества сотрудников.',
            [{'type': 'input_text', 'text': json.dumps({'hotels': data['hotels'], 'choices': data['choices']}, ensure_ascii=False)}],
            schema, 'hotel_manager_advice', limit=700)
        if response.get('status') == 'completed':
            result = response.get('data', {})
            index = result.get('choice')
            if type(index) is int and 0 <= index < len(data['choices']) and isinstance(result.get('reason'), str):
                advice.update(source='ai', text=data['choices'][index]['text'], reason=result['reason'])
    value = json.dumps(advice, ensure_ascii=False)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.flush()
    return advice


class BookingControl:
    def __init__(self, api):
        self.api = api

    async def listing(self, request):
        actor = request['miniapp_actor']
        supervisor = bool({'OWNER', 'ADMIN'} & set(actor['roles']))
        async with AsyncSession(self.api.engine) as session:
            settings = await preferences(session)
            notifications = list(await session.scalars(select(Notification).where(
                Notification.user_id == actor['id'], Notification.kind == 'manager_idle',
                Notification.resolved_at.is_(None)).order_by(Notification.id.desc()).limit(10))) if settings['enabled'] else []
            result = {'settings': settings, 'canConfigure': 'OWNER' in actor['roles'], 'supervisor': supervisor,
                      'notifications': [{'id': n.id, 'text': n.text} for n in notifications]}
            if supervisor:
                allowed = None if 'OWNER' in actor['roles'] else set(await session.scalars(select(HotelEmployee.hotel_id).where(HotelEmployee.user_id == actor['id'])))
                data = await activity(session, hotel_ids=allowed)
                saved = await session.get(Setting, f"booking-control:advice:{actor['id']}")
                result.update({k: data[k] for k in ('date', 'hotels', 'zeroBookingHotels', 'advice', 'limitations')})
                try:
                    result['aiAdvice'] = json.loads(saved.value) if saved else None
                except (ValueError, TypeError):
                    result['aiAdvice'] = None
                if result['aiAdvice'] and (not isinstance(result['aiAdvice'], dict) or result['aiAdvice'].get('fingerprint') != activity_fingerprint(data)):
                    result['aiAdvice'] = None
            return web.json_response(result)

    async def configure(self, request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        body = await self.api.body(request)
        if set(body) != {'enabled', 'idleMinutes'} or type(body['enabled']) is not bool or type(body['idleMinutes']) is not int or not 15 <= body['idleMinutes'] <= 240:
            raise AccessError('Выберите интервал от 15 до 240 минут.', 400)
        async with self.api.engine.begin() as conn:
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                               {'key': 'booking-control:settings', 'value': json.dumps(body)})
            await self.api.audit_write(conn, actor, 'booking_control_updated', 'setting', None, json.dumps(body, ensure_ascii=False))
        return web.json_response({'ok': True})


def telegram_text(message):
    """Leave the full list in the app while respecting Telegram's UTF-16 limit."""
    if len(message.encode('utf-16-le')) <= 8000:
        return message
    suffix = '\nПолный список — в приложении, раздел «Записи и помощь менеджерам».'
    budget = 8000 - len(suffix.encode('utf-16-le'))
    return message.encode('utf-16-le')[:budget].decode('utf-16-le', errors='ignore') + suffix


async def remind(session, bot, user, kind, prefix, message, now, interval, *, payload=None):
    # Operations may run in both the bot and web processes. Serialize delivery
    # for each recipient before reading the deduplication row.
    await session.get(User, user.id, with_for_update=True)
    previous = await session.scalar(select(Notification).where(
        Notification.user_id == user.id, Notification.kind == kind,
        Notification.event_key.startswith(prefix)).order_by(Notification.id.desc()).limit(1).with_for_update())
    from .push_notifications import app_channel
    in_app = app_channel()
    last_delivery = previous.created_at if previous else None
    if previous and previous.payload:
        try:
            metadata = json.loads(previous.payload)
            last_delivery = datetime.fromisoformat(metadata['queuedAt' if in_app else 'deliveredAt'])
        except (ValueError, KeyError, TypeError):
            pass
    reuse = previous and (
        previous.resolved_at is None and now - utc(last_delivery) < timedelta(minutes=interval)
        if in_app else not previous.sent or now - utc(last_delivery) < timedelta(minutes=interval))
    if reuse:
        notification = previous
        notification.text = message
        notification.resolved_at = None
    else:
        key = f'{prefix}{now.isoformat()}'
        await session.execute(text('''INSERT INTO notifications(user_id,text,sent,created_at,event_key,priority,kind)
            VALUES (:uid,:message,FALSE,:now,:key,'info',:kind) ON CONFLICT(user_id,event_key) DO NOTHING'''),
            {'uid': user.id, 'message': message, 'now': now.replace(tzinfo=None), 'key': key, 'kind': kind})
        notification = await session.scalar(select(Notification).where(
            Notification.user_id == user.id, Notification.event_key == key).with_for_update())
    if in_app:
        # Queueing an app event is not Telegram delivery or proof of phone display.
        # Keep its original queue time while refreshing current scoped details;
        # a new event is created only after the configured reminder interval.
        notification.payload = json.dumps({'url': '/app/#home', 'description': message,
            **(payload or {}), 'queuedAt': utc(last_delivery).isoformat() if reuse else now.isoformat()}, ensure_ascii=False)
        return notification.id, 0 if reuse else 1
    delivered = 0
    if notification and not notification.sent:
        try:
            async with asyncio.timeout(15):
                await bot.send_message(user.tg_id, telegram_text(message), parse_mode=None)
            notification.sent = True
            notification.payload = json.dumps({'deliveredAt': now.isoformat()})
            delivered = 1
        except (TelegramAPIError, TimeoutError):
            pass
    return notification.id, delivered


def supervisor_message(data, advice, idle):
    lines = ['Photo Boss · контроль записей', f"Дата: {data['date']}"]
    lines.append('Отели без новых записей сегодня:')
    lines.extend(f"• «{h['name']}» — на смене: {roster(h)}." for h in data['zeroBookingHotels'])
    if not data['zeroBookingHotels']:
        lines.append('Во всех доступных отелях сегодня есть новые записи.')
    lines.append('Напомните менеджерам сделать записи:')
    lines.extend(f"• {i['user'].name} · «{i['hotel']}» — {i['idleMinutes']} минут без новых записей." for i in idle)
    lines.extend(['Нужно пройти по отелю, пригласить гостей и предложить удобное время съёмки.',
                  'Рекомендация по отелям: ' + advice['text'],
                  'Переход в другой отель согласует администратор после проверки времени фотографа и потока гостей.'])
    return '\n'.join(lines)


async def activity_tick(session, bot, now):
    settings = await preferences(session)
    now = utc(now)
    if not settings['enabled']:
        await session.execute(text("UPDATE notifications SET resolved_at=:now WHERE kind IN ('manager_idle','manager_advice') AND resolved_at IS NULL"),
                              {'now': now.replace(tzinfo=None)})
        return 0
    data = await activity(session, now=now)
    current_ids = set()
    sent = 0
    for item in data['managers']:
        if item['idleMinutes'] < settings['idleMinutes']:
            continue
        message = (f"Photo Boss · напоминание\nОтель: «{item['hotel']}».\n"
                   f"Менеджеры на подтверждённой смене: {', '.join(item['managerNames'])}.\n"
                   f"У вас {item['idleMinutes']} минут без новых записей. "
                   'Пройдите по отелю, пригласите гостей на фотосессию и создайте записи на удобное время съёмки. '
                   'Если нужна помощь или переход в другой отель — свяжитесь с администратором. '
                   'Если запись уже сделана офлайн, синхронизируйте приложение.')
        nid, delivered = await remind(session, bot, item['user'], 'manager_idle',
            f"manager-idle:{item['shift'].id}:{item['lastBookingId']}:", message, now, settings['idleMinutes'],
            payload={'hotelIds': [item['hotelId']], 'shiftId': item['shift'].id, 'managerId': item['user'].id})
        current_ids.add(nid)
        sent += delivered
    admins = list(await session.scalars(select(User).join(UserRole, UserRole.user_id == User.id)
        .where(User.active.is_(True), UserRole.role.in_(['OWNER', 'ADMIN'])).distinct()))
    for user in admins:
        user_roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
        ids = None if 'OWNER' in user_roles else set(await session.scalars(select(HotelEmployee.hotel_id).where(HotelEmployee.user_id == user.id)))
        scoped = data if ids is None else await activity(session, now=now, hotel_ids=ids)
        idle = [i for i in scoped['managers'] if i['idleMinutes'] >= settings['idleMinutes']]
        if idle:
            advice = await advisor(session, user.id, scoped, now)
            nid, delivered = await remind(session, bot, user, 'manager_advice', 'manager-advice:',
                supervisor_message(scoped, advice, idle), now, settings['idleMinutes'],
                payload={'hotelIds': sorted(h['id'] for h in scoped['hotels'])})
            current_ids.add(nid)
            sent += delivered
    # New bookings, ended shifts and a resolved team situation close old alerts.
    old = list(await session.scalars(select(Notification).where(
        Notification.kind.in_(['manager_idle', 'manager_advice']), Notification.resolved_at.is_(None))))
    for notification in old:
        if notification.id not in current_ids:
            notification.resolved_at = now.replace(tzinfo=None)
    return sent


def install_booking_control(app, api):
    service = BookingControl(api)
    app.router.add_get('/api/miniapp/booking-control', service.listing)
    app.router.add_put('/api/miniapp/booking-control/settings', service.configure)
    return service
