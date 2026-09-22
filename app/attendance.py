"""Authenticated self-service shift check-in/out; same ledger as the Telegram bot.

GPS is a reported device location, NOT proof of presence at a hotel. No geofence
is claimed. No location is requested automatically or tracked in the background.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import AccessError

WORK_ROLES = frozenset({'PHOTOGRAPHER', 'MANAGER'})
MAX_JSON = 900_000
MAX_PHOTO = 650_000
LOCATION_TTL = 300


def coordinates(body):
    if set(body) != {'purpose', 'date', 'latitude', 'longitude', 'accuracy'}:
        raise AccessError('Некорректные поля геолокации.', 400)
    lat, lon, accuracy = body['latitude'], body['longitude'], body['accuracy']
    for value, limit in ((lat, 90), (lon, 180)):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
            raise AccessError('Телефон вернул некорректные координаты.', 400)
    if accuracy is not None and (type(accuracy) not in (int, float) or not math.isfinite(accuracy) or accuracy < 0):
        raise AccessError('Некорректная точность геолокации.', 400)
    return lat, lon, accuracy


def photo_bytes(body):
    if set(body) != {'purpose', 'date', 'image'}:
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
    # Telegram also validates/decompresses the image before returning a file_id.
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

    async def records(self, conn, uid, day, *, lock=False):
        suffix = ' FOR UPDATE' if lock else ''
        result = []
        for table in ('shift_check_ins', 'shift_check_outs'):
            rows = await self.api.rows(conn, f'SELECT * FROM {table} WHERE user_id=:uid AND shift_date=:day' + suffix, uid=uid, day=day)
            result.append(dict(rows[0]) if rows else None)
        return result

    def result(self, actor, start, end, now):
        def part(row, photo_key, at_key):
            if not row:
                return None
            received = row.get('location_received_at')
            fresh = received is not None and 0 <= (now - utc(received)).total_seconds() <= LOCATION_TTL
            return {'status': row['status'], 'locationSaved': row.get('latitude') is not None,
                    'locationFresh': fresh, 'photoSaved': bool(row.get(photo_key)),
                    'claimedAt': str(row['offline_claimed_at']) if row.get('offline_claimed_at') else None,
                    'at': utc(row[at_key]).astimezone(self.api.tz).isoformat() if row.get(at_key) else None}
        return {'date': now.astimezone(self.api.tz).date().isoformat(), 'timezone': str(self.api.tz),
                'eligible': bool(WORK_ROLES & set(actor['roles'])),
                'start': part(start, 'full_body_file_id', 'started_at'),
                'end': part(end, 'workplace_file_id', 'ended_at'),
                'late': bool(start and start.get('late')),
                'fine': float(start.get('fine_amount') or 0) if start else 0,
                'geoPolicy': 'coordinates_only',
                'geoNotice': 'Координаты сохраняются. Проверка расстояния до отеля пока не настроена.',
                'startTime': '09:00', 'lateFine': 500}

    async def status(self, request):
        actor, now = request['miniapp_actor'], self.now()
        async with self.api.engine.connect() as conn:
            start, end = await self.records(conn, actor['id'], now.astimezone(self.api.tz).date())
        return web.json_response(self.result(actor, start, end, now))

    async def location(self, request):
        body = await self.payload(request)
        lat, lon, accuracy = coordinates(body)
        now = self.now()
        day = now.astimezone(self.api.tz).date()
        table = 'shift_check_ins' if body['purpose'] == 'start' else 'shift_check_outs'
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            start, end = await self.records(conn, actor['id'], day, lock=True)
            if body['purpose'] == 'end' and (not start or start['status'] != 'STARTED'):
                raise AccessError('Сначала подтвердите начало смены.', 409)
            row = start if body['purpose'] == 'start' else end
            if row and row['status'] in ('STARTED', 'FINISHED', 'PENDING_REVIEW'):
                raise AccessError('Эта отметка уже подтверждена. Обновите экран.', 409)
            if row is None:
                extra_columns, extra_values = (',late,fine_amount', ',FALSE,0') if body['purpose'] == 'start' else ('', '')
                await conn.execute(text(f'''INSERT INTO {table}
                    (user_id,shift_date,status,initiated_at{extra_columns})
                    VALUES (:uid,:day,'AWAITING_LOCATION',:now{extra_values})
                    ON CONFLICT (user_id,shift_date) DO NOTHING'''),
                    {'uid': actor['id'], 'day': day, 'now': now.replace(tzinfo=None)})
                start, end = await self.records(conn, actor['id'], day, lock=True)
                row = start if body['purpose'] == 'start' else end
                if row['status'] in ('STARTED', 'FINISHED', 'PENDING_REVIEW'):
                    raise AccessError('Эта отметка уже подтверждена. Обновите экран.', 409)
            await conn.execute(text(f'''UPDATE {table} SET latitude=:lat,longitude=:lon,
                location_received_at=:now,offline_claimed_at=NULL,status='AWAITING_PHOTO' WHERE id=:id'''),
                {'lat': lat, 'lon': lon, 'now': now.replace(tzinfo=None), 'id': row['id']})
            await self.api.audit_write(conn, actor, 'shift_location_received' if body['purpose']=='start' else 'shift_end_location_received',
                'shift_check_in' if body['purpose']=='start' else 'shift_check_out', row['id'],
                f"miniapp; точность устройства: {accuracy if accuracy is not None else 'неизвестна'} м; расстояние до отеля не проверялось")
            start, end = await self.records(conn, actor['id'], day)
        return web.json_response(self.result(actor, start, end, self.now()))

    async def send_photo(self, actor, raw, purpose):
        from aiogram.types import BufferedInputFile
        title = 'Начало смены · фото в полный рост' if purpose == 'start' else 'Завершение смены · рабочее место'
        async with asyncio.timeout(15):
            message = await self.api.bot.send_photo(actor['tg_id'], BufferedInputFile(raw, filename='shift.jpg'),
                caption=f'Photo Boss · {title}', protect_content=True)
        return message.photo[-1].file_id

    async def photo(self, request):
        body = await self.payload(request, photo=True)
        raw = photo_bytes(body)
        now = self.now()
        day = now.astimezone(self.api.tz).date()
        is_start = body['purpose'] == 'start'
        final = 'STARTED' if is_start else 'FINISHED'
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            start, end = await self.records(conn, actor['id'], day, lock=True)
            row = start if is_start else end
            if not is_start and (not start or start['status'] != 'STARTED'):
                raise AccessError('Сначала подтвердите начало смены.', 409)
            if row and row['status'] == final:
                return web.json_response(self.result(actor, start, end, now))
            if not row or row['status'] != 'AWAITING_PHOTO' or row.get('latitude') is None or not row.get('location_received_at'):
                raise AccessError('Сначала отправьте геолокацию.', 409)
            if not 0 <= (now - utc(row['location_received_at'])).total_seconds() <= LOCATION_TTL:
                raise AccessError('Геолокация устарела. Получите её повторно.', 409)
            # The database row lock serializes Mini App and existing bot confirmations.
            file_id = await self.send_photo(actor, raw, body['purpose'])
            if self.now().astimezone(self.api.tz).date() != day:
                raise AccessError('Рабочая дата изменилась. Обновите экран.', 409)
            await self.confirm(conn, actor, row, body['purpose'], day, now, file_id)
            start, end = await self.records(conn, actor['id'], day)
        return web.json_response(self.result(actor, start, end, self.now()))

    async def confirm(self, conn, actor, row, purpose, day, when, file_id):
        """Caller holds the original attendance row lock; exactly one late fee."""
        from .services.shifts import LATE_FINE, is_late
        is_start = purpose == 'start'
        table, field, final, at = attendance_fields(purpose)
        late = bool(is_start and is_late(when))
        fine = LATE_FINE if late else 0
        extra = ',late=:late,fine_amount=:fine' if is_start else ''
        await conn.execute(text(f'UPDATE {table} SET {field}=:file,{at}=:now,status=:status{extra} WHERE id=:id'),
            {'file': file_id, 'now': when.replace(tzinfo=None), 'status': final, 'late': late, 'fine': fine, 'id': row['id']})
        if late:
            await conn.execute(text('''INSERT INTO payroll_entries (user_id,kind,amount,period,note,created_at)
                VALUES (:uid,'Штраф за опоздание',:amount,:period,:note,:now)'''),
                {'uid': row['user_id'], 'amount': -fine, 'period': day.isoformat(),
                 'note': f'Начало смены после 09:00; shift_check_in={row["id"]}; miniapp', 'now': self.now().replace(tzinfo=None)})
        await self.api.audit_write(conn, actor, 'shift_started' if is_start else 'shift_finished',
            'shift_check_in' if is_start else 'shift_check_out', row['id'],
            f'miniapp; геолокация и фото сохранены; штраф={fine:g}')

    async def offline(self, conn, actor, day, data, raw):
        if not WORK_ROLES & set(actor['roles']):
            raise AccessError('Отметка доступна фотографу или менеджеру.', 403)
        if set(data) != {'purpose', 'claimedAt', 'latitude', 'longitude', 'accuracy'} or data['purpose'] not in ('start', 'end'):
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
        start, end = await self.records(conn, actor['id'], day, lock=True)
        row = start if data['purpose'] == 'start' else end
        if row and row['status'] == final:
            return {'attendanceStatus': final, 'alreadyConfirmed': True}
        if row and row['status'] == 'PENDING_REVIEW':
            raise AccessError('Отметка уже ожидает проверки. Не создавайте повторную.', 409)
        if data['purpose'] == 'end' and (not start or start['status'] not in ('STARTED', 'PENDING_REVIEW')):
            raise AccessError('Сначала синхронизируйте начало смены.', 424)
        if row is None:
            extra, values = (',late,fine_amount', ',FALSE,0') if data['purpose'] == 'start' else ('', '')
            await conn.execute(text(f'INSERT INTO {table}(user_id,shift_date,status,initiated_at{extra}) VALUES (:uid,:day,\'AWAITING_LOCATION\',:now{values})'),
                {'uid': actor['id'], 'day': day, 'now': now.replace(tzinfo=None)})
            start, end = await self.records(conn, actor['id'], day, lock=True)
            row = start if data['purpose'] == 'start' else end
        user = (await self.api.rows(conn, 'SELECT tg_id FROM users WHERE id=:id', id=actor['id']))[0]
        file_id = await self.send_photo(actor | {'tg_id': user['tg_id']}, raw, data['purpose'])
        await conn.execute(text(f'''UPDATE {table} SET latitude=:lat,longitude=:lon,{field}=:file,
            location_received_at=:now,offline_claimed_at=:claimed,status='PENDING_REVIEW' WHERE id=:id'''),
            {'id': row['id'], 'lat': lat, 'lon': lon, 'file': file_id, 'now': now.replace(tzinfo=None), 'claimed': claimed.replace(tzinfo=None)})
        await self.api.audit_write(conn, actor, 'offline_attendance_received', table, row['id'],
            json.dumps({'claimedAt': claimed.isoformat(), 'receivedAt': now.isoformat(), 'accuracy': accuracy,
                        'note': 'Время устройства и присутствие не подтверждены; штраф не начислен.'}, ensure_ascii=False))
        return {'attendanceStatus': 'PENDING_REVIEW', 'attendanceId': row['id']}

    async def pending(self, request):
        from .people import check_editor
        check_editor(request['miniapp_actor']['roles'])
        items = []
        async with self.api.engine.connect() as conn:
            for purpose in ('start', 'end'):
                table, _, _, _ = attendance_fields(purpose)
                rows = await self.api.rows(conn, f'''SELECT s.id,s.user_id,u.name,s.shift_date,s.offline_claimed_at,s.location_received_at,s.latitude,s.longitude
                    FROM {table} s JOIN users u ON u.id=s.user_id WHERE s.status='PENDING_REVIEW' ORDER BY s.shift_date,s.id LIMIT 200''')
                items += [dict(r) | {'purpose': purpose, 'shift_date': str(r['shift_date']),
                    'offline_claimed_at': utc(r['offline_claimed_at']).isoformat(), 'location_received_at': utc(r['location_received_at']).isoformat()} for r in rows]
        return web.json_response({'items': items})

    async def evidence(self, request):
        from .academy_practice import BoundedPhoto
        from .people import check_editor, positive_id
        check_editor(request['miniapp_actor']['roles'])
        table, field, _, _ = attendance_fields(request.match_info['purpose'])
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, f'SELECT {field} AS photo FROM {table} WHERE id=:id AND offline_claimed_at IS NOT NULL', id=positive_id(request.match_info['id']))
        if not rows or not rows[0]['photo']:
            raise AccessError('Фото не найдено.', 404)
        buffer = BoundedPhoto()
        await self.api.bot.download(rows[0]['photo'], destination=buffer, timeout=25)
        return web.json_response({'image': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}, headers={'Cache-Control': 'no-store'})

    async def review(self, request):
        from .people import People, check_editor, positive_id
        actor = request['miniapp_actor']
        check_editor(actor['roles'])
        body = await self.api.body(request)
        if set(body) != {'userId', 'date', 'purpose', 'approved', 'verifiedAt', 'note'} or type(body['approved']) is not bool or not isinstance(body['note'], str) or not 3 <= len(body['note'].strip()) <= 1000:
            raise AccessError('Укажите решение и основание проверки.', 400)
        uid = positive_id(body['userId'])
        day = self.api.day(body['date'])
        table, field, final, at = attendance_fields(body['purpose'])
        when = explicit_time(body['verifiedAt']) if body['approved'] else None
        if when and (when.astimezone(self.api.tz).date() != day or when > self.now()):
            raise AccessError('Подтверждённое время должно относиться к дню смены и не быть будущим.', 400)
        async with self.api.engine.begin() as conn:
            await People(self.api).current_editor(conn, actor['id'], uid)
            start, end = await self.records(conn, uid, day, lock=True)
            row = start if body['purpose'] == 'start' else end
            if not row or not row['offline_claimed_at']:
                raise AccessError('Офлайн-отметка не найдена.', 404)
            if (row['status'] == final and when and utc(row[at]) == when) or (row['status'] == 'REJECTED' and not when):
                return web.json_response({'ok': True, 'alreadyReviewed': True})
            if row['status'] != 'PENDING_REVIEW':
                raise AccessError('Отметка уже рассмотрена. Обновите список.', 409)
            if when:
                if body['purpose'] == 'end' and (not start or start['status'] != 'STARTED' or when < utc(start['started_at'])):
                    raise AccessError('Сначала подтвердите начало; завершение должно быть позже него.', 409)
                await self.confirm(conn, actor, row, body['purpose'], day, when, row[field])
            else:
                await conn.execute(text(f"UPDATE {table} SET status='REJECTED' WHERE id=:id"), {'id': row['id']})
            await self.api.audit_write(conn, actor, 'offline_attendance_reviewed', table, row['id'],
                json.dumps({'before': 'PENDING_REVIEW', 'after': final if when else 'REJECTED',
                            'verifiedAt': when.isoformat() if when else None, 'note': body['note'].strip()}, ensure_ascii=False))
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
