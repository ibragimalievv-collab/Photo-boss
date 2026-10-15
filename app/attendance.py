"""Authenticated self-service shift check-in/out; same ledger as the Telegram bot.

GPS is a reported device location, NOT proof of presence at a hotel. No geofence
is claimed. No location is requested automatically or tracked in the background.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError, utc_bounds
from .services.attendance_ai import analyze_checkin, inspect_jpeg
from .services.geofence import check_hotel_geofence

WORK_ROLES = frozenset({'PHOTOGRAPHER', 'MANAGER'})
MAX_JSON = 900_000
MAX_PHOTO = 650_000
LOCATION_TTL = 300


def coordinates(body):
    if set(body) not in ({'purpose', 'date', 'latitude', 'longitude', 'accuracy'}, {'purpose', 'date', 'latitude', 'longitude', 'accuracy', 'shiftId'}):
        raise AccessError('Некорректные поля геолокации.', 400)
    lat, lon, accuracy = body['latitude'], body['longitude'], body['accuracy']
    for value, limit in ((lat, 90), (lon, 180)):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
            raise AccessError('Телефон вернул некорректные координаты.', 400)
    if accuracy is not None and (type(accuracy) not in (int, float) or not math.isfinite(accuracy) or accuracy < 0):
        raise AccessError('Некорректная точность геолокации.', 400)
    return lat, lon, accuracy


def photo_bytes(body):
    if set(body) not in ({'purpose', 'date', 'image'}, {'purpose', 'date', 'image', 'shiftId'}):
        raise AccessError('Некорректные поля фотографии.', 400)
    value = body['image']
    if not isinstance(value, str) or not value.startswith('data:image/jpeg;base64,'):
        raise AccessError('Нужна фотография в формате JPEG.', 400)
    try:
        raw = base64.b64decode(value.split(',', 1)[1], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AccessError('Не удалось прочитать фотографию.', 400) from exc
    if len(raw) > MAX_PHOTO:
        raise AccessError('Фотография слишком большая. Сделайте снимок заново.', 413)
    if len(raw) < 100 or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9'):
        raise AccessError('Некорректная фотография JPEG.', 400)
    try:
        inspect_jpeg(raw)
    except ValueError as exc:
        raise AccessError('Не удалось прочитать фотографию JPEG. Сделайте новый снимок.', 400) from exc
    return raw


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Attendance:
    def __init__(self, miniapp):
        self.api = miniapp

    def now(self):
        return datetime.now(timezone.utc)

    async def payload(self, request, *, photo=False):
        limit = MAX_JSON if photo else 8192
        if request.content_length and request.content_length > limit:
            raise AccessError('Слишком большой запрос.', 413)
        raw = bytearray()
        while not request.content.at_eof():
            raw.extend(await request.content.read(min(65536, limit + 1 - len(raw))))
            if len(raw) > limit:
                raise AccessError('Слишком большой запрос.', 413)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AccessError('Некорректный запрос.', 400) from exc
        if not isinstance(value, dict):
            raise AccessError('Некорректный запрос.', 400)
        if value.get('purpose') not in ('start', 'end'):
            raise AccessError('Неизвестное действие смены.', 400)
        if value.get('date') != self.now().astimezone(self.api.tz).date().isoformat():
            raise AccessError('Рабочая дата изменилась. Обновите экран смены.', 409)
        return value

    async def locked_actor(self, conn, request):
        actor = request['miniapp_actor']
        people = await self.api.rows(conn, 'SELECT id,active FROM users WHERE id=:uid FOR UPDATE', uid=actor['id'])
        roles = await self.api.rows(conn, 'SELECT role FROM user_roles WHERE user_id=:uid', uid=actor['id'])
        if not people or not people[0]['active'] or not any(x['role'] in WORK_ROLES for x in roles):
            raise AccessError('Личная отметка доступна активному фотографу или менеджеру записи.')
        return actor

    async def scope(self, conn, uid, day, requested=None, *, at=None):
        lo, hi = utc_bounds(day, day, self.api.tz)
        schedules = [dict(row) for row in await self.api.rows(conn, """SELECT s.*,h.name AS hotel_name,h.latitude AS hotel_latitude,h.longitude AS hotel_longitude,h.attendance_radius_m AS hotel_radius_m
            FROM shifts s JOIN hotels h ON h.id=s.hotel_id WHERE s.user_id=:uid
            AND s.start_at>=:lo AND s.start_at<:hi AND s.status<>'CANCELLED' ORDER BY s.start_at,s.id""",
            uid=uid, lo=lo, hi=hi)]
        if requested is not None:
            if type(requested) is not int or not 0 < requested < 2**31:
                raise AccessError('Некорректный номер смены.', 400)
            selected = next((row for row in schedules if row['id'] == requested), None)
            if not selected:
                raise AccessError('Смена недоступна или была отменена. Обновите график.', 409)
            return selected, schedules
        if not schedules:
            return None, []
        now = at or self.now()
        starts = await self.api.rows(conn, "SELECT scheduled_shift_id,status FROM shift_check_ins WHERE user_id=:uid AND shift_date=:day", uid=uid, day=day)
        ends = await self.api.rows(conn, "SELECT scheduled_shift_id,status FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day", uid=uid, day=day)
        closed = {row['scheduled_shift_id'] for row in ends if row['status'] == 'FINISHED'}
        active = {row['scheduled_shift_id'] for row in starts if row['status'] in ('STARTED','PENDING_REVIEW')} - closed
        selected = next((row for row in schedules if row['id'] in active), None)
        if selected is None:
            selected = next((row for row in schedules if row['id'] not in closed and utc(row['start_at']) <= now <= utc(row['end_at'])), None)
        if selected is None:
            selected = next((row for row in schedules if row['id'] not in closed and utc(row['start_at']) > now), None)
        return selected or schedules[-1], schedules

    def geofence(self, selected, lat, lon, accuracy):
        point = {'latitude': selected.get('hotel_latitude'), 'longitude': selected.get('hotel_longitude'),
                 'attendance_radius_m': selected.get('hotel_radius_m')} if selected else {}
        try:
            return check_hotel_geofence(point, lat, lon, accuracy)
        except ValueError as exc:
            raise AccessError(str(exc), 400) from exc

    async def records(self, conn, uid, day, *, lock=False, shift_id=None):
        suffix = ' FOR UPDATE' if lock else ''
        result = []
        scope = 'scheduled_shift_id=:sid' if shift_id is not None else 'scheduled_shift_id IS NULL'
        for table in ('shift_check_ins', 'shift_check_outs'):
            rows = await self.api.rows(conn, f'SELECT * FROM {table} WHERE user_id=:uid AND shift_date=:day AND {scope}' + suffix, uid=uid, day=day, sid=shift_id)
            result.append(dict(rows[0]) if rows else None)
        return result

    def result(self, actor, start, end, now, selected=None, schedules=()):
        def part(row, photo_key, at_key):
            if not row:
                return None
            received = row.get('location_received_at')
            fresh = received is not None and 0 <= (now - utc(received)).total_seconds() <= LOCATION_TTL
            status = 'AWAITING_PHOTO' if photo_key == 'full_body_file_id' and row['status'] == 'STARTED' and row.get('ai_status') not in ('ACCEPTED','MANUAL_APPROVED') else row['status']
            note = row.get('ai_note')
            if status != row['status'] and not note:
                note = 'Для продолжения работы подтвердите смену новым снимком с ИИ-проверкой. Прежнее время начала и штраф сохранятся.'
            return {'status': status, 'aiStatus': row.get('ai_status'), 'aiNote': note, 'locationSaved': row.get('latitude') is not None,
                    'locationFresh': fresh, 'photoSaved': bool(row.get(photo_key)),
                    'claimedAt': str(row['offline_claimed_at']) if row.get('offline_claimed_at') else None,
                    'at': utc(row[at_key]).astimezone(self.api.tz).isoformat() if row.get(at_key) else None}
        return {'date': now.astimezone(self.api.tz).date().isoformat(), 'timezone': str(self.api.tz),
                'eligible': bool(WORK_ROLES & set(actor['roles'])),
                'start': part(start, 'full_body_file_id', 'started_at'),
                'end': part(end, 'workplace_file_id', 'ended_at'),
                'late': bool(start and start.get('late')),
                'fine': float(start.get('fine_amount') or 0) if start else 0,
                'geoPolicy': 'hotel_radius' if selected and selected.get('hotel_latitude') is not None else 'coordinates_only',
                'geoNotice': (f"Местоположение проверяется относительно отеля. Радиус: {selected['hotel_radius_m']:g} м." if selected and selected.get('hotel_latitude') is not None else 'Координаты сохраняются. Проверка расстояния до отеля пока не настроена.'),
                'shiftId': selected['id'] if selected else None,
                'hotelId': selected['hotel_id'] if selected else None,
                'hotelName': selected['hotel_name'] if selected else None,
                'shifts': [{'id': row['id'], 'hotelId': row['hotel_id'], 'hotelName': row['hotel_name'],
                            'startAt': utc(row['start_at']).astimezone(self.api.tz).isoformat(),
                            'endAt': utc(row['end_at']).astimezone(self.api.tz).isoformat(), 'status': row['status']} for row in schedules],
                'startTime': utc(selected['start_at']).astimezone(self.api.tz).strftime('%H:%M') if selected else '09:00', 'lateFine': 500}

    async def status(self, request):
        actor, now = request['miniapp_actor'], self.now()
        requested = request.query.get('shiftId')
        if requested is not None:
            try:
                requested = int(requested)
            except (ValueError, TypeError):
                raise AccessError('Некорректный номер смены.', 400) from None
        day = now.astimezone(self.api.tz).date()
        async with self.api.engine.connect() as conn:
            selected, schedules = await self.scope(conn, actor['id'], day, requested)
            sid = selected['id'] if selected else None
            start, end = await self.records(conn, actor['id'], day, shift_id=sid)
        return web.json_response(self.result(actor, start, end, now, selected, schedules))

    async def location(self, request):
        body = await self.payload(request)
        lat, lon, accuracy = coordinates(body)
        now = self.now()
        day = now.astimezone(self.api.tz).date()
        table = 'shift_check_ins' if body['purpose'] == 'start' else 'shift_check_outs'
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            selected, schedules = await self.scope(conn, actor['id'], day, body.get('shiftId'))
            sid = selected['id'] if selected else None
            geo = self.geofence(selected, lat, lon, accuracy)
            if not geo['accepted']:
                raise AccessError(geo['message'], 409)
            start, end = await self.records(conn, actor['id'], day, lock=True, shift_id=sid)
            if body['purpose'] == 'end' and (not start or start['status'] != 'STARTED'):
                raise AccessError('Сначала подтвердите начало этой смены.', 409)
            row = start if body['purpose'] == 'start' else end
            if row and row['status'] in ('STARTED', 'FINISHED', 'PENDING_REVIEW') and not (body['purpose'] == 'start' and row['status'] == 'STARTED' and row.get('ai_status') not in ('ACCEPTED','MANUAL_APPROVED')):
                raise AccessError('Эта отметка уже подтверждена. Обновите экран.', 409)
            if row is None:
                extra_columns, extra_values = (',late,fine_amount', ',FALSE,0') if body['purpose'] == 'start' else ('', '')
                await conn.execute(text(f"""INSERT INTO {table}
                    (user_id,shift_date,scheduled_shift_id,status,initiated_at{extra_columns})
                    VALUES (:uid,:day,:sid,'AWAITING_LOCATION',:now{extra_values})
                    ON CONFLICT DO NOTHING"""),
                    {'uid': actor['id'], 'day': day, 'sid': sid, 'now': now.replace(tzinfo=None)})
                start, end = await self.records(conn, actor['id'], day, lock=True, shift_id=sid)
                row = start if body['purpose'] == 'start' else end
            await conn.execute(text(f"""UPDATE {table} SET latitude=:lat,longitude=:lon,location_accuracy=:accuracy,
                location_received_at=:now,offline_claimed_at=NULL,status='AWAITING_PHOTO' WHERE id=:id"""),
                {'lat': lat, 'lon': lon, 'accuracy': accuracy, 'now': now.replace(tzinfo=None), 'id': row['id']})
            await self.api.audit_write(conn, actor, 'shift_location_received' if body['purpose']=='start' else 'shift_end_location_received',
                'shift_check_in' if body['purpose']=='start' else 'shift_check_out', row['id'],
                f"miniapp; shift={sid}; точность устройства: {accuracy if accuracy is not None else 'неизвестна'} м; geofence={geo['status']}; distance_m={geo['distance_m']}")
            start, end = await self.records(conn, actor['id'], day, shift_id=sid)
        return web.json_response(self.result(actor, start, end, self.now(), selected, schedules))

    async def send_photo(self, actor, raw, purpose):
        # Evidence is stored durably with the attendance record. Web employees
        # need no reachable Telegram chat and no public photo URL is created.
        return 'attendance-db:' + secrets.token_hex(20)

    async def photo(self, request):
        body = await self.payload(request, photo=True)
        raw = photo_bytes(body)
        now = self.now()
        day = now.astimezone(self.api.tz).date()
        is_start = body['purpose'] == 'start'
        final = 'STARTED' if is_start else 'FINISHED'
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            selected, schedules = await self.scope(conn, actor['id'], day, body.get('shiftId'))
            sid = selected['id'] if selected else None
            start, end = await self.records(conn, actor['id'], day, lock=True, shift_id=sid)
            row = start if is_start else end
            if not is_start and (not start or start['status'] != 'STARTED'):
                raise AccessError('Сначала подтвердите начало этой смены.', 409)
            if row and row['status'] == final and not (is_start and row.get('ai_status') not in ('ACCEPTED','MANUAL_APPROVED')):
                return web.json_response(self.result(actor, start, end, now, selected, schedules))
            if not row or (row['status'] != 'AWAITING_PHOTO' and not (is_start and row['status'] == 'STARTED' and row.get('ai_status') not in ('ACCEPTED','MANUAL_APPROVED'))) or row.get('latitude') is None or not row.get('location_received_at'):
                raise AccessError('Сначала отправьте геолокацию.', 409)
            if not 0 <= (now - utc(row['location_received_at'])).total_seconds() <= LOCATION_TTL:
                raise AccessError('Геолокация устарела. Получите её повторно.', 409)
            geo = self.geofence(selected, row['latitude'], row['longitude'], row.get('location_accuracy'))
            if not geo['accepted']:
                raise AccessError(geo['message'], 409)
            # User lock serializes API, reviews and shooting operations. A failed AI
            # response commits its retry state without confirming or charging fees.
            analysis = await analyze_checkin(raw) if is_start else None
            file_id = await self.send_photo(actor, raw, body['purpose'])
            if self.now().astimezone(self.api.tz).date() != day:
                raise AccessError('Рабочая дата изменилась. Обновите экран.', 409)
            if is_start:
                await conn.execute(text("""UPDATE shift_check_ins SET full_body_file_id=:file,
                    ai_status=:status,ai_note=:note,ai_checked_at=:now,photo_data=:photo_data WHERE id=:id"""),
                    {'file': file_id, 'status': analysis['status'], 'note': analysis['note'],
                     'now': self.now().replace(tzinfo=None), 'id': row['id'], 'photo_data': base64.b64encode(raw).decode('ascii')})
                await self.api.audit_write(conn, actor, 'shift_ai_checked', 'shift_check_in', row['id'],
                    json.dumps({'status': analysis['status'], 'note': analysis['note'], 'shiftId': sid}, ensure_ascii=False))
            if not is_start:
                await conn.execute(text('UPDATE shift_check_outs SET photo_data=:photo_data WHERE id=:id'),
                    {'photo_data': base64.b64encode(raw).decode('ascii'), 'id': row['id']})
            if not is_start or analysis['status'] == 'ACCEPTED':
                await self.confirm(conn, actor, row, body['purpose'], day, now, file_id, selected=selected)
            start, end = await self.records(conn, actor['id'], day, shift_id=sid)
        return web.json_response(self.result(actor, start, end, self.now(), selected, schedules))

    async def confirm(self, conn, actor, row, purpose, day, when, file_id, *, selected=None, manual=False):
        """Caller holds user and attendance row locks; one fee per scoped start."""
        from .services.shifts import LATE_FINE, is_late
        is_start = purpose == 'start'
        table, field, final, at = attendance_fields(purpose)
        if selected is None and row.get('scheduled_shift_id') is not None:
            selected, _ = await self.scope(conn, row['user_id'], day, row['scheduled_shift_id'], at=when)
        was_confirmed = bool(is_start and row.get('started_at'))
        if was_confirmed:
            when = utc(row['started_at'])
        late = bool(row.get('late')) if was_confirmed else bool(is_start and is_late(when, selected['start_at'] if selected else None))
        fine = float(row.get('fine_amount') or 0) if was_confirmed else LATE_FINE if late else 0
        extra = ',late=:late,fine_amount=:fine' if is_start else ''
        if is_start and manual:
            extra += ",ai_status='MANUAL_APPROVED',ai_note=:manual_note,ai_checked_at=:reviewed"
        await conn.execute(text(f'UPDATE {table} SET {field}=:file,{at}=:now,status=:status{extra} WHERE id=:id'),
            {'file': file_id, 'now': when.replace(tzinfo=None), 'status': final, 'late': late, 'fine': fine,
             'id': row['id'], 'manual_note': 'Начало подтверждено руководителем по офлайн-доказательствам; ИИ не подтверждал снимок.',
             'reviewed': self.now().replace(tzinfo=None)})
        scheduled_time = utc(selected['start_at']).astimezone(self.api.tz).strftime('%H:%M') if selected else '09:00'
        if late and not was_confirmed:
            await conn.execute(text("""INSERT INTO payroll_entries (user_id,kind,amount,period,note,created_at)
                VALUES (:uid,'Штраф за опоздание',:amount,:period,:note,:now)"""),
                {'uid': row['user_id'], 'amount': -fine, 'period': day.isoformat(),
                 'note': f'Начало смены после {scheduled_time}; shift_check_in={row["id"]}; miniapp', 'now': self.now().replace(tzinfo=None)})
        await self.api.audit_write(conn, actor, 'shift_started' if is_start else 'shift_finished',
            'shift_check_in' if is_start else 'shift_check_out', row['id'],
            f'miniapp; shift={row.get("scheduled_shift_id")}; геолокация и фото сохранены; штраф={fine:g}; подтверждение={"руководитель (офлайн)" if manual else "AI" if is_start else "фото рабочего места"}')

    async def offline(self, conn, actor, day, data, raw):
        if not WORK_ROLES & set(actor['roles']):
            raise AccessError('Отметка доступна фотографу или менеджеру.', 403)
        if set(data) not in ({'purpose', 'claimedAt', 'latitude', 'longitude', 'accuracy'}, {'purpose', 'claimedAt', 'latitude', 'longitude', 'accuracy', 'shiftId'}) or data['purpose'] not in ('start', 'end'):
            raise AccessError('Некорректная офлайн-отметка.', 400)
        now = self.now()
        claimed = explicit_time(data['claimedAt'])
        if claimed.astimezone(self.api.tz).date() != day or not now-timedelta(days=7) <= claimed <= now+timedelta(minutes=5):
            raise AccessError('Проверьте дату и время устройства. Отметка не применена.', 409)
        lat, lon, accuracy = coordinates({k: v for k, v in data.items() if k != 'claimedAt'} | {'date': str(day)})
        if not raw or len(raw) > MAX_PHOTO:
            raise AccessError('Прикрепите снимок живой камеры.', 400)
        photo_bytes({'purpose': data['purpose'], 'date': str(day), 'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()})
        table, field, final, _ = attendance_fields(data['purpose'])
        selected, _ = await self.scope(conn, actor['id'], day, data.get('shiftId'), at=claimed)
        sid = selected['id'] if selected else None
        start, end = await self.records(conn, actor['id'], day, lock=True, shift_id=sid)
        row = start if data['purpose'] == 'start' else end
        if row and row['status'] == final:
            return {'attendanceStatus': final, 'alreadyConfirmed': True}
        if row and row['status'] == 'PENDING_REVIEW':
            raise AccessError('Отметка уже ожидает проверки. Не создавайте повторную.', 409)
        if data['purpose'] == 'end' and (not start or start['status'] not in ('STARTED', 'PENDING_REVIEW')):
            raise AccessError('Сначала синхронизируйте начало смены.', 424)
        if row is None:
            extra, values = (',late,fine_amount', ',FALSE,0') if data['purpose'] == 'start' else ('', '')
            await conn.execute(text(f'INSERT INTO {table}(user_id,shift_date,scheduled_shift_id,status,initiated_at{extra}) VALUES (:uid,:day,:sid,\'AWAITING_LOCATION\',:now{values})'),
                {'uid': actor['id'], 'day': day, 'sid': sid, 'now': now.replace(tzinfo=None)})
            start, end = await self.records(conn, actor['id'], day, lock=True, shift_id=sid)
            row = start if data['purpose'] == 'start' else end
        if data['purpose'] == 'start':
            analysis = await analyze_checkin(raw)
            await conn.execute(text('UPDATE shift_check_ins SET ai_status=:status,ai_note=:note,ai_checked_at=:now WHERE id=:id'),
                {'status': analysis['status'], 'note': analysis['note'], 'now': now.replace(tzinfo=None), 'id': row['id']})
        user = (await self.api.rows(conn, 'SELECT tg_id FROM users WHERE id=:id', id=actor['id']))[0]
        file_id = await self.send_photo(actor | {'tg_id': user['tg_id']}, raw, data['purpose'])
        await conn.execute(text(f'''UPDATE {table} SET latitude=:lat,longitude=:lon,location_accuracy=:accuracy,{field}=:file,
            location_received_at=:now,offline_claimed_at=:claimed,photo_data=:photo_data,status='PENDING_REVIEW' WHERE id=:id'''),
            {'id': row['id'], 'lat': lat, 'lon': lon, 'accuracy': accuracy, 'file': file_id, 'now': now.replace(tzinfo=None), 'claimed': claimed.replace(tzinfo=None), 'photo_data': base64.b64encode(raw).decode('ascii')})
        await self.api.audit_write(conn, actor, 'offline_attendance_received', table, row['id'],
            json.dumps({'claimedAt': claimed.isoformat(), 'receivedAt': now.isoformat(), 'accuracy': accuracy,
                        'note': 'Время устройства и присутствие не подтверждены; штраф не начислен.'}, ensure_ascii=False))
        return {'attendanceStatus': 'PENDING_REVIEW', 'attendanceId': row['id'], 'shiftId': sid}

    async def review_scope(self, conn, actor, selected):
        if 'OWNER' in actor['roles'] or selected is None:
            return
        rows = await self.api.rows(conn, 'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid AND hotel_id=:hid',
            uid=actor['id'], hid=selected['hotel_id'])
        if not rows:
            raise AccessError('Нет доступа к смене этого отеля.', 403)

    async def pending(self, request):
        from .people import check_editor
        check_editor(request['miniapp_actor']['roles'])
        items = []
        async with self.api.engine.connect() as conn:
            for purpose in ('start', 'end'):
                table, _, _, _ = attendance_fields(purpose)
                rows = await self.api.rows(conn, f'''SELECT s.id,s.user_id,u.name,s.shift_date,s.scheduled_shift_id,s.offline_claimed_at,s.location_received_at,s.latitude,s.longitude
                    FROM {table} s JOIN users u ON u.id=s.user_id WHERE s.status='PENDING_REVIEW' ORDER BY s.shift_date,s.id LIMIT 200''')
                if 'OWNER' not in request['miniapp_actor']['roles']:
                    allowed = {r['hotel_id'] for r in await self.api.rows(conn, 'SELECT hotel_id FROM hotel_employees WHERE user_id=:uid', uid=request['miniapp_actor']['id'])}
                    plans = {r['id']: r['hotel_id'] for r in await self.api.rows(conn, 'SELECT id,hotel_id FROM shifts')}
                    staff = {r['user_id'] for r in await self.api.rows(conn, 'SELECT user_id,hotel_id FROM hotel_employees') if r['hotel_id'] in allowed}
                    rows = [r for r in rows if (plans.get(r['scheduled_shift_id']) in allowed if r['scheduled_shift_id'] is not None else r['user_id'] in staff)]
                items += [dict(r) | {'purpose': purpose, 'shiftId': r['scheduled_shift_id'], 'shift_date': str(r['shift_date']),
                    'offline_claimed_at': utc(r['offline_claimed_at']).isoformat(), 'location_received_at': utc(r['location_received_at']).isoformat()} for r in rows]
        return web.json_response({'items': items})

    async def evidence(self, request):
        from .academy_practice import BoundedPhoto
        from .people import check_editor, positive_id
        check_editor(request['miniapp_actor']['roles'])
        table, field, _, _ = attendance_fields(request.match_info['purpose'])
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, f'SELECT {field} AS photo,photo_data,user_id,shift_date,scheduled_shift_id FROM {table} WHERE id=:id AND offline_claimed_at IS NOT NULL', id=positive_id(request.match_info['id']))
            if rows:
                from .people import People
                await People(self.api).current_editor(conn, request['miniapp_actor']['id'], rows[0]['user_id'])
                selected = None
                if rows[0]['scheduled_shift_id'] is not None:
                    selected, _ = await self.scope(conn, rows[0]['user_id'], self.api.day(str(rows[0]['shift_date'])), rows[0]['scheduled_shift_id'])
                await self.review_scope(conn, request['miniapp_actor'], selected)
        if not rows or not rows[0]['photo']:
            raise AccessError('Фото не найдено.', 404)
        if rows[0].get('photo_data'):
            return web.json_response({'image': 'data:image/jpeg;base64,' + rows[0]['photo_data']}, headers={'Cache-Control': 'no-store'})
        if rows[0]['photo'].startswith('attendance-db:'):
            raise AccessError('Снимок недоступен. Повторите отправку.', 409)
        buffer = BoundedPhoto()
        await self.api.bot.download(rows[0]['photo'], destination=buffer, timeout=25)
        return web.json_response({'image': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}, headers={'Cache-Control': 'no-store'})

    async def review(self, request):
        from .people import People, check_editor, positive_id
        actor = request['miniapp_actor']
        check_editor(actor['roles'])
        body = await self.api.body(request)
        if set(body) not in ({'userId', 'date', 'purpose', 'approved', 'verifiedAt', 'note'}, {'userId', 'date', 'purpose', 'approved', 'verifiedAt', 'note', 'shiftId'}) or type(body['approved']) is not bool or not isinstance(body['note'], str) or not 3 <= len(body['note'].strip()) <= 1000:
            raise AccessError('Укажите решение и основание проверки.', 400)
        uid = positive_id(body['userId'])
        day = self.api.day(body['date'])
        table, field, final, at = attendance_fields(body['purpose'])
        when = explicit_time(body['verifiedAt']) if body['approved'] else None
        if when and (when.astimezone(self.api.tz).date() != day or when > self.now()):
            raise AccessError('Подтверждённое время должно относиться к дню смены и не быть будущим.', 400)
        async with self.api.engine.begin() as conn:
            await People(self.api).current_editor(conn, actor['id'], uid)
            # Serialize against normal check-in/checkout and work operations.
            await self.api.rows(conn, 'SELECT id FROM users WHERE id=:uid FOR UPDATE', uid=uid)
            selected, schedules = await self.scope(conn, uid, day, body.get('shiftId'), at=when)
            if len(schedules) > 1 and 'shiftId' not in body:
                raise AccessError('Выберите конкретную смену для проверки.', 409)
            sid = selected['id'] if selected else None
            await self.review_scope(conn, actor, selected)
            start, end = await self.records(conn, uid, day, lock=True, shift_id=sid)
            row = start if body['purpose'] == 'start' else end
            if not row or not row['offline_claimed_at']:
                raise AccessError('Офлайн-отметка не найдена.', 404)
            if (row['status'] == final and when and utc(row[at]) == when) or (row['status'] == 'REJECTED' and not when):
                return web.json_response({'ok': True, 'alreadyReviewed': True})
            if row['status'] != 'PENDING_REVIEW':
                raise AccessError('Отметка уже рассмотрена. Обновите список.', 409)
            if when:
                geo = self.geofence(selected, row['latitude'], row['longitude'], row.get('location_accuracy'))
                if not geo['accepted']:
                    raise AccessError(geo['message'], 409)
                if body['purpose'] == 'end' and (not start or start['status'] != 'STARTED' or when < utc(start['started_at'])):
                    raise AccessError('Сначала подтвердите начало; завершение должно быть позже него.', 409)
                await self.confirm(conn, actor, row, body['purpose'], day, when, row[field], selected=selected, manual=True)
            else:
                await conn.execute(text(f"UPDATE {table} SET status='REJECTED' WHERE id=:id"), {'id': row['id']})
            await self.api.audit_write(conn, actor, 'offline_attendance_reviewed', table, row['id'],
                json.dumps({'before': 'PENDING_REVIEW', 'after': final if when else 'REJECTED',
                            'verifiedAt': when.isoformat() if when else None, 'note': body['note'].strip(),
                            'shiftId': sid, 'manualApproval': bool(when), 'previousAiStatus': row.get('ai_status')}, ensure_ascii=False))
        return web.json_response({'ok': True})


def attendance_fields(purpose):
    if purpose not in ('start', 'end'):
        raise AccessError('Неизвестное действие смены.', 400)
    return ('shift_check_ins', 'full_body_file_id', 'STARTED', 'started_at') if purpose == 'start' else ('shift_check_outs', 'workplace_file_id', 'FINISHED', 'ended_at')


def explicit_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise AccessError('Время должно включать часовой пояс.', 400) from exc


def install_attendance(app, miniapp):
    service = Attendance(miniapp)
    app.router.add_get('/api/miniapp/attendance', service.status)
    app.router.add_post('/api/miniapp/attendance/location', service.location)
    app.router.add_post('/api/miniapp/attendance/photo', service.photo)
    app.router.add_get('/api/miniapp/attendance/pending', service.pending)
    app.router.add_get('/api/miniapp/attendance/evidence/{purpose}/{id}', service.evidence)
    app.router.add_post('/api/miniapp/attendance/review', service.review)
    root = Path(__file__).parent / 'attendance_ui'
    async def script(_request):
        return web.FileResponse(root / 'attendance.js', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    async def style(_request):
        return web.FileResponse(root / 'attendance.css', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    app.router.add_get('/shift/attendance.js', script)
    app.router.add_get('/shift/attendance.css', style)
    return service
