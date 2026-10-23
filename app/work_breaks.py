"""Recorded lunch intervals with server time, hotel scope and audited corrections."""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError, utc_bounds
from .models import (
    Hotel,
    HotelEmployee,
    Shift,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    WorkBreak,
    utc_now,
)
from .services.core import audit, roles_of


async def hotel_access(session, actor_id, roles, hotel_id, *, manage=False):
    hotel = await session.get(Hotel, hotel_id)
    if not hotel or not hotel.active:
        raise AccessError('Отель недоступен.', 404)
    if 'OWNER' in roles:
        return
    if manage and 'ADMIN' not in roles:
        raise AccessError('Изменять данные могут владелец и администратор.', 403)
    if not await session.scalar(select(HotelEmployee.id).where(
            HotelEmployee.user_id == actor_id, HotelEmployee.hotel_id == hotel_id).limit(1)):
        raise AccessError('Нет доступа к этому отелю.', 403)


def stamp(value):
    return value.isoformat() + 'Z' if value else None


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        raise AccessError('Укажите дату и время с часовым поясом.', 400) from None


class WorkBreaks:
    def __init__(self, api):
        self.api = api

    async def actor(self, session, actor_id):
        actor = await session.get(User, actor_id)
        if not actor or not actor.active:
            raise AccessError('Доступ отключён.', 403)
        return actor, await roles_of(session, actor)

    async def record(self, conn, actor_data, day, kind, data):
        if set(data) != {'hotelId'} or type(data['hotelId']) is not int:
            raise AccessError('Выберите отель смены.', 400)
        if day != self.api.today():
            raise AccessError('Обед отмечается текущим временем. Исправление прошлых отметок доступно руководителю.', 409)
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            actor, roles = await self.actor(session, actor_data['id'])
            check = await session.scalar(select(ShiftCheckIn).where(
                ShiftCheckIn.user_id == actor.id, ShiftCheckIn.shift_date == day))
            from .attendance import start_recorded
            ended = await session.scalar(select(ShiftCheckOut.id).where(
                ShiftCheckOut.user_id == actor.id, ShiftCheckOut.shift_date == day,
                ShiftCheckOut.status.in_(['FINISHED', 'PENDING_REVIEW'])))
            if kind != 'break_end' and (not start_recorded(check.__dict__ if check else None) or ended):
                raise AccessError('Сначала начните смену; завершённую смену менять нельзя.', 409)
            now = utc_now()
            current = await session.scalar(select(WorkBreak).where(
                WorkBreak.user_id == actor.id, WorkBreak.ended_at.is_(None)).with_for_update())
            if kind == 'break_end':
                if not current or current.hotel_id != data['hotelId']:
                    raise AccessError('Открытая отметка обеда не найдена.', 409)
                current.ended_at = max(now, current.started_at)
                current.updated_at = now
            else:
                if current:
                    raise AccessError('Вы уже на обеде. Сначала отметьте возвращение.', 409)
                # A plan is authoritative even when memberships are incomplete.
                planned = await session.scalar(select(Shift.id).where(
                    Shift.user_id == actor.id, Shift.hotel_id == data['hotelId'],
                    Shift.status != 'CANCELLED', Shift.start_at <= now, Shift.end_at > now).limit(1))
                if not planned:
                    await hotel_access(session, actor.id, roles, data['hotelId'])
                current = WorkBreak(user_id=actor.id, hotel_id=data['hotelId'], shift_date=day,
                                    started_at=now, updated_at=now)
                session.add(current)
            await session.flush()
            await audit(session, actor, kind, 'work_break', current.id,
                        json.dumps({'hotelId': current.hotel_id, 'start': stamp(current.started_at),
                                    'end': stamp(current.ended_at)}, ensure_ascii=False))
            from .config import config
            from .push_notifications import queue_notice, scoped_supervisors
            hotel = await session.get(Hotel, current.hotel_id)
            phase = 'lunch_end' if kind == 'break_end' else 'lunch_start'
            local_time = now.replace(tzinfo=UTC).astimezone(ZoneInfo(config.training_timezone))
            description = (f'Сотрудник: {actor.name}\nОтель: {hotel.name if hotel else "не указан"}\n'
                           f'{"Вернулся с обеда" if kind == "break_end" else "Ушёл на обед"}: {local_time:%H:%M}')
            recipients = await scoped_supervisors(session, current.hotel_id) | {actor.id}
            for uid in sorted(recipients):
                recipient = await session.get(User, uid)
                if recipient and recipient.active:
                    await queue_notice(session, uid, kind=phase, event_key=f'work-break:{current.id}:{phase}',
                        message=description, payload={'workBreakId': current.id, 'description': description})
            return {'breakId': current.id, 'startedAt': stamp(current.started_at), 'endedAt': stamp(current.ended_at)}

    async def listing(self, request):
        a = request['miniapp_actor']
        day = self.api.day(getattr(request, 'query', {}).get('date', str(self.api.today())))
        async with AsyncSession(self.api.engine) as session:
            actor, roles = await self.actor(session, a['id'])
            editor = bool(roles & {'OWNER', 'ADMIN'})
            hotels = list(await session.scalars(select(Hotel).where(Hotel.active.is_(True))))
            allowed = set(await session.scalars(select(HotelEmployee.hotel_id).where(HotelEmployee.user_id == actor.id)))
            lo, hi = utc_bounds(day, day, self.api.tz)
            planned = set(await session.scalars(select(Shift.hotel_id).where(Shift.user_id == actor.id,
                Shift.status != 'CANCELLED', Shift.start_at < hi, Shift.end_at > lo)))
            query = select(WorkBreak).where((WorkBreak.shift_date == day) |
                ((WorkBreak.user_id == actor.id) & WorkBreak.ended_at.is_(None)))
            if 'OWNER' not in roles:
                query = query.where(WorkBreak.hotel_id.in_(allowed) | (WorkBreak.user_id == actor.id)) if editor else query.where(WorkBreak.user_id == actor.id)
            rows = list(await session.scalars(query.order_by(WorkBreak.started_at)))
            result = []
            for row in rows:
                user = await session.get(User, row.user_id)
                hotel = await session.get(Hotel, row.hotel_id)
                result.append({'id': row.id, 'userId': row.user_id, 'name': user.name,
                    'hotelId': row.hotel_id, 'hotel': hotel.name, 'startedAt': stamp(row.started_at),
                    'endedAt': stamp(row.ended_at), 'revision': stamp(row.updated_at),
                    'minutes': int(((row.ended_at or utc_now()) - row.started_at).total_seconds() // 60)})
            employee_query = select(User).where(User.active.is_(True))
            if 'OWNER' not in roles:
                employee_query = employee_query.where(User.id.in_(select(HotelEmployee.user_id).where(HotelEmployee.hotel_id.in_(allowed))))
            employees = list(await session.scalars(employee_query)) if editor else []
            from .attendance import start_recorded
            check = await session.scalar(select(ShiftCheckIn).where(
                ShiftCheckIn.user_id == actor.id, ShiftCheckIn.shift_date == day))
            ended = await session.scalar(select(ShiftCheckOut.id).where(
                ShiftCheckOut.user_id == actor.id, ShiftCheckOut.shift_date == day,
                ShiftCheckOut.status.in_(['FINISHED', 'PENDING_REVIEW'])))
            can_start = start_recorded(check.__dict__ if check else None) and not ended
        return web.json_response({'date': str(day), 'timezone': str(self.api.tz), 'actorId': actor.id,
            'canEdit': editor, 'canStart': bool(can_start),
            'startPendingReview': bool(check and check.status == 'PENDING_REVIEW'), 'items': result,
            'hotels': [{'id': h.id, 'name': h.name} for h in hotels if 'OWNER' in roles or h.id in (allowed | planned)],
            'employees': [{'id': u.id, 'name': u.name} for u in employees]})

    async def save(self, request):
        a, body = request['miniapp_actor'], await self.api.body(request)
        creating = 'id' not in request.match_info
        expected = {'userId', 'hotelId', 'date', 'startedAt', 'endedAt', 'reason'} if creating else {'startedAt', 'endedAt', 'reason', 'revision'}
        if set(body) != expected or not isinstance(body['reason'], str) or not 3 <= len(body['reason'].strip()) <= 500:
            raise AccessError('Укажите время и причину исправления (3–500 символов).', 400)
        start, end = parse_time(body['startedAt']), parse_time(body['endedAt']) if body['endedAt'] else None
        if (end and end < start) or start > utc_now() or (end and end > utc_now()):
            raise AccessError('Проверьте время обеда: окончание не раньше начала, время не в будущем.', 400)
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor, roles = await self.actor(session, a['id'])
            if not roles & {'OWNER', 'ADMIN'}:
                raise AccessError('Исправлять обед могут владелец и администратор.', 403)
            if creating:
                uid, hid = body['userId'], body['hotelId']
                if type(uid) is not int or type(hid) is not int:
                    raise AccessError('Выберите сотрудника и отель.', 400)
                employee = await session.get(User, uid, with_for_update={'key_share': True})
                if not employee or not employee.active:
                    raise AccessError('Сотрудник недоступен.', 404)
                day = self.api.day(body['date'])
                row = WorkBreak(user_id=uid, hotel_id=hid, shift_date=day, started_at=start)
                await hotel_access(session, actor.id, roles, hid, manage=True)
                if 'OWNER' not in roles:
                    await hotel_access(session, uid, set(), hid)
                session.add(row)
                before = None
            else:
                row = await session.get(WorkBreak, int(request.match_info['id']))
                if not row:
                    raise AccessError('Отметка не найдена.', 404)
                await hotel_access(session, actor.id, roles, row.hotel_id, manage=True)
                # Serialize overlapping changes against self-service actions too.
                await session.get(User, row.user_id, with_for_update={'key_share': True})
                row = await session.get(WorkBreak, row.id, with_for_update=True, populate_existing=True)
                if body['revision'] != stamp(row.updated_at):
                    raise AccessError('Отметка уже изменена. Обновите данные.', 409)
                before = {'startedAt': stamp(row.started_at), 'endedAt': stamp(row.ended_at)}
            if start.replace(tzinfo=UTC).astimezone(self.api.tz).date() != row.shift_date or (end and end - start > timedelta(hours=24)):
                raise AccessError('Начало обеда должно относиться к выбранной смене; интервал — не более суток.', 400)
            overlap = await session.scalar(select(WorkBreak.id).where(WorkBreak.user_id == row.user_id,
                WorkBreak.id != (row.id or -1), *([WorkBreak.started_at < end] if end else []),
                (WorkBreak.ended_at.is_(None) | (WorkBreak.ended_at > start))).limit(1))
            if overlap:
                raise AccessError('Этот интервал пересекается с другим обедом сотрудника.', 409)
            row.started_at, row.ended_at, row.updated_at = start, end, utc_now()
            await session.flush()
            await audit(session, actor, 'break_corrected', 'work_break', row.id,
                json.dumps({'before': before, 'after': {'startedAt': stamp(start), 'endedAt': stamp(end)}, 'reason': body['reason'].strip()}, ensure_ascii=False))
            await session.commit()
        return web.json_response({'ok': True, 'id': row.id})


async def close_breaks(session, user_id, day, when, actor):
    """Forgotten return is explicitly recorded as shift-end, never guessed."""
    rows = list(await session.scalars(select(WorkBreak).where(WorkBreak.user_id == user_id,
        WorkBreak.shift_date == day, WorkBreak.ended_at.is_(None)).with_for_update()))
    for row in rows:
        if when < row.started_at:
            raise AccessError('Завершение смены раньше отметки обеда. Сначала исправьте время обеда.', 409)
        row.ended_at, row.updated_at = when, utc_now()
        await audit(session, actor, 'break_closed_with_shift', 'work_break', row.id,
                    'Окончание по завершению смены; отдельная отметка возвращения отсутствует.')


def install_breaks(app, api):
    service = WorkBreaks(api)
    app.router.add_get('/api/miniapp/work-breaks', service.listing)
    app.router.add_post('/api/miniapp/work-breaks', service.save)
    app.router.add_put('/api/miniapp/work-breaks/{id:\\d+}', service.save)
