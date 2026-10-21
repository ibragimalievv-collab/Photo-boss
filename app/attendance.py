"""Authenticated self-service shift check-in/out; same ledger as the Telegram bot.

GPS is reported device evidence, NOT proof of presence. Fresh online evidence
inside one configured hotel zone can record a shift automatically; ambiguous
location and offline evidence remain available for human review.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from sqlalchemy import text

from .hotel_points import capture_context, context_key, read_json, review_required
from .miniapp_security import AccessError, utc_bounds

WORK_ROLES = frozenset({'PHOTOGRAPHER', 'MANAGER'})
MAX_JSON = 900_000
MAX_PHOTO = 650_000
LOCATION_TTL = 300


def gps_required():
    """Only an explicit owner-configured 0 temporarily disables GPS checks."""
    return os.getenv('PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED', '1').strip() != '0'


def start_recorded(row):
    """Allow work after submitted evidence without claiming human approval."""
    return bool(row and (row['status'] == 'STARTED' or (
        row['status'] == 'PENDING_REVIEW' and row.get('full_body_file_id')
        and (not gps_required() or (row.get('latitude') is not None and row.get('longitude') is not None))
        and row.get('offline_claimed_at'))))


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
    if review_required() or not gps_required():
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(BytesIO(raw)) as image:
                if image.format != 'JPEG' or min(image.size) < 96 or image.width * image.height > 4_000_000:
                    raise ValueError
                image.load()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise AccessError('Фотография повреждена или слишком мала. Сделайте новый снимок.', 400) from exc
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

    async def day_in_scope(self, conn, actor, user_id, day):
        """Attendance has no hotel FK: only that day's plan can establish scope.

        Shared or unplanned days stay owner-only; membership/GPS cannot prove
        which hotel's attendance and possible fine is being reviewed.
        """
        if 'OWNER' in actor['roles']:
            return True
        lo, hi = utc_bounds(day, day, self.api.tz)
        rows = await self.api.rows(conn, '''SELECT s.hotel_id,
            CASE WHEN EXISTS (SELECT 1 FROM hotel_employees he
                WHERE he.user_id=:actor AND he.hotel_id=s.hotel_id) THEN 1 ELSE 0 END AS allowed
            FROM shifts s WHERE s.user_id=:uid AND s.status<>'CANCELLED'
            AND s.start_at<:hi AND s.end_at>:lo''',
            actor=actor['id'], uid=user_id, lo=lo, hi=hi)
        return bool(rows) and all(r['allowed'] for r in rows)

    async def require_day_scope(self, conn, actor, user_id, day):
        if not await self.day_in_scope(conn, actor, user_id, day):
            raise AccessError('Нет однозначной привязки отметки к назначенным вам отелям. '
                'Проверить её может владелец; нужен план смены за этот день.', 403)

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
            row = dict(rows[0]) if rows else None
            if row:
                row['geofence'] = await read_json(self.api, conn, context_key('start' if table == 'shift_check_ins' else 'end', row['id']))
            result.append(row)
        return result

    def result(self, actor, start, end, now):
        def part(row, photo_key, at_key):
            if not row:
                return None
            received = row.get('location_received_at')
            fresh = received is not None and 0 <= (now - utc(received)).total_seconds() <= LOCATION_TTL
            return {'status': row['status'], 'locationSaved': row.get('latitude') is not None,
                    'locationFresh': fresh, 'photoSaved': bool(row.get(photo_key)),
                    'locationReceivedAt': utc(received).isoformat() if received else None,
                    'claimedAt': str(row['offline_claimed_at']) if row.get('offline_claimed_at') else None,
                    'geofence': row.get('geofence'),
                    'evidenceSource': (row.get('geofence') or {}).get('source'),
                    'at': utc(row[at_key]).astimezone(self.api.tz).isoformat() if row.get(at_key) else None}
        return {'date': now.astimezone(self.api.tz).date().isoformat(), 'timezone': str(self.api.tz),
                'eligible': bool(WORK_ROLES & set(actor['roles'])),
                'start': part(start, 'full_body_file_id', 'started_at'),
                'end': part(end, 'workplace_file_id', 'ended_at'),
                'late': bool(start and start.get('late')),
                'fine': float(start.get('fine_amount') or 0) if start else 0,
                'reviewRequired': review_required(), 'gpsRequired': gps_required(),
                'geoPolicy': 'temporarily_disabled' if not gps_required() else 'geofence_review' if review_required() else 'coordinates_only',
                'geoNotice': ('GPS временно отключён по решению владельца. Местоположение не проверяется; для отметки нужно свежее фото камеры.' if not gps_required() else 'При свежих координатах внутри зоны одного отеля и сохранённом фото смена подтверждается автоматически. При неточных координатах, вне зоны или без настроенной зоны нужна проверка руководителя. Личность и форму автоматически не проверяем.'
                              if review_required() else 'Координаты сохраняются. Проверка расстояния до отеля пока не настроена.'),
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
            if body['purpose'] == 'end' and not start_recorded(start):
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
            if review_required():
                await capture_context(self.api, conn, actor['id'], day, body['purpose'], row['id'], lat, lon, accuracy)
            await self.api.audit_write(conn, actor, 'shift_location_received' if body['purpose']=='start' else 'shift_end_location_received',
                'shift_check_in' if body['purpose']=='start' else 'shift_check_out', row['id'],
                f"miniapp; точность устройства: {accuracy if accuracy is not None else 'неизвестна'} м; политика={'geofence_review' if review_required() else 'coordinates_only'}")
            start, end = await self.records(conn, actor['id'], day)
        return web.json_response(self.result(actor, start, end, self.now()))

    async def send_photo(self, actor, raw, purpose):
        from .push_notifications import app_channel
        if app_channel():
            from .services.media_files import save_private_media
            from .yandex_disk import YandexDiskError
            try:
                path, _ = await save_private_media(actor['id'], raw)
            except YandexDiskError as exc:
                raise AccessError('Сохранение фото смены на Диске не подтверждено. Повторите отправку.', 503) from exc
            return path
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
        if not gps_required():
            return await self.photo_without_gps(request, body, raw, now, day)
        is_start = body['purpose'] == 'start'
        final = 'STARTED' if is_start else 'FINISHED'
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            start, end = await self.records(conn, actor['id'], day, lock=True)
            row = start if is_start else end
            if not is_start and not start_recorded(start):
                raise AccessError('Сначала подтвердите начало смены.', 409)
            if row and row['status'] in (final, 'PENDING_REVIEW'):
                return web.json_response(self.result(actor, start, end, now))
            if not row or row['status'] != 'AWAITING_PHOTO' or row.get('latitude') is None or not row.get('location_received_at'):
                raise AccessError('Сначала отправьте геолокацию.', 409)
            if not 0 <= (now - utc(row['location_received_at'])).total_seconds() <= LOCATION_TTL:
                raise AccessError('Геолокация устарела. Получите её повторно.', 409)
            # The database row lock serializes Mini App and existing bot confirmations.
            file_id = await self.send_photo(actor, raw, body['purpose'])
            if self.now().astimezone(self.api.tz).date() != day:
                raise AccessError('Рабочая дата изменилась. Обновите экран.', 409)
            if review_required():
                table, field, _, _ = attendance_fields(body['purpose'])
                # Keep submitted evidence for leadership inspection even when
                # the technical checks allow immediate confirmation.
                await conn.execute(text(f'''UPDATE {table} SET {field}=:file,
                    offline_claimed_at=:now,status='PENDING_REVIEW' WHERE id=:id'''),
                    {'file': file_id, 'now': now.replace(tzinfo=None), 'id': row['id']})
                # The hotel plan/zone may have changed while the camera or Disk
                # upload was open. Re-check current reference data before auto approval.
                previous_context = row.get('geofence') or {}
                context = await capture_context(self.api, conn, actor['id'], day, body['purpose'],
                    row['id'], row['latitude'], row['longitude'], previous_context.get('accuracyM'))
                automatic = (context.get('state') == 'inside' and context.get('source') == 'online'
                             and 0 <= (self.now()-utc(row['location_received_at'])).total_seconds() <= LOCATION_TTL
                             and (is_start or start['status'] == 'STARTED'))
                if automatic:
                    await self.confirm(conn, actor, row, body['purpose'], day, now, file_id)
                    await self.api.audit_write(conn, actor, 'attendance_auto_confirmed', table, row['id'],
                        'Свежие координаты в зоне назначенного отеля; JPEG проверен и сохранён. Личность и форма автоматически не проверялись.')
                else:
                    await self.api.audit_write(conn, actor, 'attendance_review_requested', table, row['id'],
                        'Фото и координаты сохранены; нужна проверка руководителя. Расчёт времени автоматический.')
                    from .attendance_fines import reconcile_day
                    await reconcile_day(self.api, conn, actor['id'], day, now)
            else:
                await self.confirm(conn, actor, row, body['purpose'], day, now, file_id)
            await self.notify_submission(conn, actor, body['purpose'], row['id'], day)
            start, end = await self.records(conn, actor['id'], day)
        return web.json_response(self.result(actor, start, end, self.now()))

    async def photo_without_gps(self, request, body, raw, now, day):
        """Explicit temporary policy: a fresh upload, no assertion of location."""
        purpose = body['purpose']
        is_start = purpose == 'start'
        table, field, final, _at = attendance_fields(purpose)
        async with self.api.engine.begin() as conn:
            actor = await self.locked_actor(conn, request)
            start, end = await self.records(conn, actor['id'], day, lock=True)
            row = start if is_start else end
            if not is_start and not start_recorded(start):
                raise AccessError('Сначала сохраните начало смены.', 409)
            if row and row['status'] == final:
                return web.json_response(self.result(actor, start, end, now))
            previous = (row or {}).get('geofence') or {}
            renewed = bool(row and row['status'] == 'PENDING_REVIEW')
            if renewed and previous.get('source') == 'offline':
                raise AccessError('Офлайн-отметка ожидает проверки руководителя; время устройства нельзя подтвердить автоматически.', 409)
            if row is None:
                extra, values = (',late,fine_amount', ',FALSE,0') if is_start else ('', '')
                await conn.execute(text(f'''INSERT INTO {table}
                    (user_id,shift_date,status,initiated_at{extra})
                    VALUES (:uid,:day,'AWAITING_PHOTO',:now{values})
                    ON CONFLICT(user_id,shift_date) DO NOTHING'''),
                    {'uid': actor['id'], 'day': day, 'now': now.replace(tzinfo=None)})
                start, end = await self.records(conn, actor['id'], day, lock=True)
                row = start if is_start else end
            when = now
            # Only a complete prior ONLINE upload has a trusted server receipt
            # time. A new photograph unblocks it without turning the retry delay
            # into extra lateness. Never trust an offline/unknown device claim.
            complete = bool(row.get(field) and row.get('offline_claimed_at') and
                ((row.get('latitude') is not None and row.get('longitude') is not None
                  and row.get('location_received_at')) or previous.get('gpsRequired') is False))
            if renewed and complete and previous.get('source') == 'online':
                original = utc(row['offline_claimed_at'])
                if original.astimezone(self.api.tz).date() == day and original <= now:
                    when = original
            if not is_start and start.get('started_at') and when < utc(start['started_at']):
                raise AccessError('Сохранённое завершение раньше начала смены. Время должен проверить руководитель.', 409)
            file_id = await self.send_photo(actor, raw, purpose)
            if self.now().astimezone(self.api.tz).date() != day:
                raise AccessError('Рабочая дата изменилась. Обновите экран.', 409)
            if renewed:
                await self.api.audit_write(conn, actor, 'attendance_photo_resubmitted', table, row['id'],
                    json.dumps({'previousPhoto': row.get(field),
                        'previousClaimedAt': str(row.get('offline_claimed_at')),
                        'previousContext': previous, 'photoReceivedAt': now.isoformat(),
                        'recordedAt': when.isoformat(), 'gpsRequired': False}, ensure_ascii=False))
                await conn.execute(text('''UPDATE notifications SET resolved_at=:now
                    WHERE kind='attendance' AND event_key LIKE :prefix AND resolved_at IS NULL'''),
                    {'now': now.replace(tzinfo=None), 'prefix': f'attendance:{purpose}:{row["id"]}:%'})
            await conn.execute(text(f'''UPDATE {table} SET {field}=:file,
                offline_claimed_at=:when,status='PENDING_REVIEW' WHERE id=:id'''),
                {'file': file_id, 'when': when.replace(tzinfo=None), 'id': row['id']})
            await capture_context(self.api, conn, actor['id'], day, purpose, row['id'],
                None, None, None, source='online', gps_disabled=True)
            automatic = is_start or start['status'] == 'STARTED'
            if automatic:
                await self.confirm(conn, actor, row, purpose, day, when, file_id)
            else:
                from .attendance_fines import reconcile_day
                await reconcile_day(self.api, conn, actor['id'], day, now)
            await self.api.audit_write(conn, actor, 'attendance_gps_temporarily_disabled', table, row['id'],
                'По решению владельца GPS временно не требуется. Свежий JPEG проверен и сохранён; местоположение не проверялось. '
                + ('Отметка подтверждена автоматически.' if automatic else 'Начало смены ещё требует ручной проверки.'))
            await self.notify_submission(conn, actor, purpose, row['id'], day, renewed=renewed)
            start, end = await self.records(conn, actor['id'], day)
        return web.json_response(self.result(actor, start, end, self.now()))

    async def notify_submission(self, conn, actor, purpose, row_id, day, *, worker_id=None, decision=None, renewed=False):
        """Persist submissions and review decisions with the same scoped access."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession

        from .models import ShiftCheckIn, ShiftCheckOut, User, UserRole
        from .push_notifications import queue_notice
        model = ShiftCheckIn if purpose == 'start' else ShiftCheckOut
        worker_id = actor['id'] if worker_id is None else worker_id
        # A review mutation is serialized by the attendance row lock. Exact
        # retries return before reaching here, while later corrections each
        # need a new event even if they restore an earlier recorded time.
        event = f'review:{uuid4().hex}' if decision else f'resubmitted:{uuid4().hex}' if renewed else 'submitted'
        async with AsyncSession(bind=conn, expire_on_commit=False) as session:
            row = await session.get(model, row_id)
            user = await session.get(User, worker_id)
            context = await read_json(self.api, conn, context_key(purpose, row_id)) or {}
            recipients = {worker_id}
            candidates = set(await session.scalars(select(UserRole.user_id).where(UserRole.role.in_(['OWNER', 'ADMIN']))))
            for uid in candidates:
                roles = set(await session.scalars(select(UserRole.role).where(UserRole.user_id == uid)))
                if await self.day_in_scope(conn, {'id': uid, 'roles': roles}, worker_id, day):
                    recipients.add(uid)
            when = row.started_at if purpose == 'start' else row.ended_at
            recorded = when or row.offline_claimed_at
            local = utc(recorded).astimezone(self.api.tz) if recorded else None
            time_label = f'{day:%d.%m.%Y} {local:%H:%M}' if local else f'{day:%d.%m.%Y}; время не сохранено'
            automatic_notice = ('Подтверждено автоматически по свежему фото; GPS временно отключён, местоположение не проверялось'
                if context.get('gpsRequired') is False else 'Подтверждено автоматически по фото и координатам')
            description = (f'Сотрудник: {user.name}\n'
                           f'{"Начало" if purpose == "start" else "Завершение"} смены: {time_label}\n'
                           f'Отель: {context.get("hotelName", "нужна проверка привязки")}\n'
                           f'{automatic_notice if row.status in {"STARTED", "FINISHED"} else "Нужна проверка руководителя"}.')
            if decision:
                reviewer = await session.get(User, actor['id'])
                outcome = ('Отметка отклонена руководителем' if row.status == 'REJECTED' else
                           'Время отметки исправлено руководителем' if decision['corrected'] else
                           'Отметка подтверждена руководителем')
                description = (f'{outcome}.\nСотрудник: {user.name}\n'
                    f'{"Начало" if purpose == "start" else "Завершение"} смены: {time_label}\n'
                    f'Отель: {context.get("hotelName", "нужна проверка привязки")}\n'
                    f'Проверил: {reviewer.name}\nОснование: {decision["note"]}')
                if context.get('gpsRequired') is False:
                    description += '\nGPS при получении отметки был временно отключён; местоположение автоматически не проверялось.'
            for uid in sorted(recipients):
                recipient = await session.get(User, uid)
                if recipient and recipient.active:
                    await queue_notice(session, uid, kind='attendance', event_key=f'attendance:{purpose}:{row_id}:{event}',
                        url='/app/#workday', message=description,
                        payload={'attendanceId': row_id, 'purpose': purpose, 'userId': worker_id,
                                 'date': str(day), 'hotelId': context.get('hotelId'), 'status': row.status,
                                 'reviewed': decision is not None,
                                 'description': description})
            await session.flush()

    async def confirm(self, conn, actor, row, purpose, day, when, file_id):
        """Caller holds the original attendance row lock; time charges reconcile once."""
        from .services.shifts import LATE_FINE, is_late
        is_start = purpose == 'start'
        table, field, final, at = attendance_fields(purpose)
        correcting = row['status'] == final
        gps_waived = not gps_required() or (row.get('geofence') or {}).get('gpsRequired') is False
        modern_policy = review_required() or gps_waived
        late = bool(is_start and is_late(when) and not modern_policy)
        fine = LATE_FINE if late else 0
        extra = ',late=:late,fine_amount=:fine' if is_start else ''
        await conn.execute(text(f'UPDATE {table} SET {field}=:file,{at}=:now,status=:status{extra} WHERE id=:id'),
            {'file': file_id, 'now': when.replace(tzinfo=None), 'status': final, 'late': late, 'fine': fine, 'id': row['id']})
        if not is_start:
            from sqlalchemy.ext.asyncio import AsyncSession

            from .models import User
            from .work_breaks import close_breaks
            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                await close_breaks(session, row['user_id'], day, when.replace(tzinfo=None), await session.get(User, actor['id']))
                await session.flush()
        if late and not correcting:
            await conn.execute(text('''INSERT INTO payroll_entries (user_id,kind,amount,period,note,created_at)
                VALUES (:uid,'Штраф за опоздание',:amount,:period,:note,:now)'''),
                {'uid': row['user_id'], 'amount': -fine, 'period': day.isoformat(),
                 'note': f'Начало смены после 09:00; shift_check_in={row["id"]}; miniapp', 'now': self.now().replace(tzinfo=None)})
        if modern_policy or correcting:
            from .attendance_fines import reconcile_day
            await reconcile_day(self.api, conn, row['user_id'], day, self.now())
        await self.api.audit_write(conn, actor, 'shift_started' if is_start else 'shift_finished',
            'shift_check_in' if is_start else 'shift_check_out', row['id'],
            ('miniapp; фото сохранено; при получении отметки GPS не требовался, местоположение не проверялось; начисления рассчитаны по сохранённому времени'
             if gps_waived else f'miniapp; геолокация и фото сохранены; штраф={fine:g}'))

    async def offline(self, conn, actor, day, data, raw):
        if not WORK_ROLES & set(actor['roles']):
            raise AccessError('Отметка доступна фотографу или менеджеру.', 403)
        if set(data) != {'purpose', 'claimedAt', 'latitude', 'longitude', 'accuracy'} or data['purpose'] not in ('start', 'end'):
            raise AccessError('Некорректная офлайн-отметка.', 400)
        now = self.now()
        claimed = explicit_time(data['claimedAt'])
        if claimed.astimezone(self.api.tz).date() != day or not now-timedelta(days=7) <= claimed <= now+timedelta(minutes=5):
            raise AccessError('Проверьте дату и время устройства. Отметка не применена.', 409)
        without_gps = not gps_required() and all(data[key] is None for key in ('latitude', 'longitude', 'accuracy'))
        lat, lon, accuracy = (None, None, None) if without_gps else coordinates(
            {k: v for k, v in data.items() if k != 'claimedAt'} | {'date': str(day)})
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
        if data['purpose'] == 'end' and not start_recorded(start):
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
            {'id': row['id'], 'lat': lat, 'lon': lon, 'file': file_id, 'now': None if without_gps else now.replace(tzinfo=None), 'claimed': claimed.replace(tzinfo=None)})
        if review_required() or not gps_required():
            await capture_context(self.api, conn, actor['id'], day, data['purpose'], row['id'], lat, lon, accuracy,
                source='offline', gps_disabled=not gps_required())
            from .attendance_fines import reconcile_day
            await reconcile_day(self.api, conn, actor['id'], day, now)
        await self.api.audit_write(conn, actor, 'offline_attendance_received', table, row['id'],
            json.dumps({'claimedAt': claimed.isoformat(), 'receivedAt': now.isoformat(), 'accuracy': accuracy,
                        'note': 'Время устройства и присутствие не подтверждены; автоматический расчёт исправляет руководитель при уважительной причине.'}, ensure_ascii=False))
        await self.notify_submission(conn, actor, data['purpose'], row['id'], day)
        return {'attendanceStatus': 'PENDING_REVIEW', 'attendanceId': row['id']}

    async def pending(self, request):
        from .people import People, check_editor
        check_editor(request['miniapp_actor']['roles'])
        actor, items = request['miniapp_actor'], []
        async with self.api.engine.connect() as conn:
            actor = actor | {'roles': await People(self.api).current_editor(conn, actor['id'])}
            scopes = {}
            for purpose in ('start', 'end'):
                table, _, final, at = attendance_fields(purpose)
                # Filter before the visible-page limit. Foreign hotels must not
                # fill the first page and hide an administrator's own requests.
                visible = 0
                # Pending evidence always takes precedence over recent history.
                for status in ('PENDING_REVIEW', final):
                    cursor = None
                    while visible < 200:
                        where = '' if cursor is None else 'AND (s.shift_date>:cursor_day OR (s.shift_date=:cursor_day AND s.id>:cursor_id))'
                        history = '' if status == 'PENDING_REVIEW' else 'AND s.shift_date>=:since AND s.offline_claimed_at IS NOT NULL'
                        rows = await self.api.rows(conn, f'''SELECT s.id,s.user_id,u.name,s.shift_date,s.status,s.{at} AS confirmed_at,s.offline_claimed_at,s.location_received_at,s.latitude,s.longitude
                            FROM {table} s JOIN users u ON u.id=s.user_id WHERE s.status=:status
                            {history} {where} ORDER BY s.shift_date,s.id LIMIT 200''',
                            status=status, since=self.now().astimezone(self.api.tz).date()-timedelta(days=7),
                            **({'cursor_day': cursor[0], 'cursor_id': cursor[1]} if cursor else {}))
                        if not rows:
                            break
                        for r in rows:
                            day = datetime.fromisoformat(str(r['shift_date'])).date()
                            key = r['user_id'], day
                            if key not in scopes:
                                scopes[key] = await self.day_in_scope(conn, actor, key[0], day)
                            if scopes[key]:
                                items.append(dict(r) | {'purpose': purpose, 'shift_date': str(r['shift_date']),
                                    'offline_claimed_at': utc(r['offline_claimed_at']).isoformat() if r['offline_claimed_at'] else None,
                                    'location_received_at': utc(r['location_received_at']).isoformat() if r['location_received_at'] else None,
                                    'confirmedAt': utc(r['confirmed_at']).isoformat() if r['confirmed_at'] else None,
                                    'geofence': await read_json(self.api, conn, context_key(purpose, r['id']))})
                                visible += 1
                                if visible == 200:
                                    break
                        cursor = rows[-1]['shift_date'], rows[-1]['id']
                        if len(rows) < 200:
                            break
        return web.json_response({'items': sorted(items, key=lambda r: (r['status'] != 'PENDING_REVIEW', r['shift_date'], r['id'])),
            'scopeNotice': '' if 'OWNER' in actor['roles'] else
                'Показаны отметки со сменами только в назначенных вам отелях. Без плана или с другими отелями проверяет владелец.'})

    async def evidence(self, request):
        from .people import People, check_editor, positive_id
        from .services.media_files import load_private_media
        check_editor(request['miniapp_actor']['roles'])
        table, field, _, _ = attendance_fields(request.match_info['purpose'])
        async with self.api.engine.connect() as conn:
            actor = request['miniapp_actor'] | {'roles': await People(self.api).current_editor(conn, request['miniapp_actor']['id'])}
            rows = await self.api.rows(conn, f'SELECT user_id,shift_date,{field} AS photo FROM {table} WHERE id=:id AND offline_claimed_at IS NOT NULL', id=positive_id(request.match_info['id']))
            if rows:
                await self.require_day_scope(conn, actor, rows[0]['user_id'],
                    datetime.fromisoformat(str(rows[0]['shift_date'])).date())
        if not rows or not rows[0]['photo']:
            raise AccessError('Фото не найдено.', 404)
        raw = await load_private_media(self.api.bot, rows[0]['photo'], max_bytes=MAX_PHOTO, timeout=25)
        return web.json_response({'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()}, headers={'Cache-Control': 'no-store'})

    async def review(self, request):
        from .people import People, check_editor, positive_id
        actor = request['miniapp_actor']
        check_editor(actor['roles'])
        body = await self.api.body(request)
        fields = {'userId', 'date', 'purpose', 'approved', 'verifiedAt', 'note'}
        if set(body) not in (fields, fields | {'expectedAt'}) or type(body['approved']) is not bool or not isinstance(body['note'], str) or not 3 <= len(body['note'].strip()) <= 1000:
            raise AccessError('Укажите решение и основание проверки.', 400)
        uid = positive_id(body['userId'])
        if uid == actor['id']:
            raise AccessError('Свою отметку нельзя подтвердить самостоятельно. Нужен другой руководитель.', 403)
        day = self.api.day(body['date'])
        table, field, final, at = attendance_fields(body['purpose'])
        when = explicit_time(body['verifiedAt']) if body['approved'] else None
        if when and (when.astimezone(self.api.tz).date() != day or when > self.now()):
            raise AccessError('Подтверждённое время должно относиться к дню смены и не быть будущим.', 400)
        async with self.api.engine.begin() as conn:
            roles = await People(self.api).current_editor(conn, actor['id'], uid)
            await self.require_day_scope(conn, actor | {'roles': roles}, uid, day)
            start, end = await self.records(conn, uid, day, lock=True)
            row = start if body['purpose'] == 'start' else end
            if not row or (body['approved'] and not row['offline_claimed_at']):
                raise AccessError('Отметка для проверки не найдена.', 404)
            if (row['status'] == final and when and utc(row[at]) == when) or (row['status'] == 'REJECTED' and not when):
                return web.json_response({'ok': True, 'alreadyReviewed': True})
            correcting = row['status'] == final and when is not None
            if correcting and body.get('expectedAt') != utc(row[at]).isoformat():
                raise AccessError('Время уже изменено. Обновите список отметок.', 409)
            if row['status'] != 'PENDING_REVIEW' and not correcting:
                raise AccessError('Отметка уже рассмотрена. Обновите список.', 409)
            if when:
                gps_waived = (row.get('geofence') or {}).get('gpsRequired') is False
                if (not row.get(field) or (not gps_waived and (row.get('latitude') is None
                        or row.get('longitude') is None or not row.get('location_received_at')))):
                    raise AccessError('В отметке не сохранены фото и геолокация. Подтвердить её нельзя.', 409)
                if body['purpose'] == 'end' and (not start or start['status'] != 'STARTED' or when < utc(start['started_at'])):
                    raise AccessError('Сначала подтвердите начало; завершение должно быть позже него.', 409)
                if body['purpose'] == 'start' and end and end['status'] == 'FINISHED' and when > utc(end['ended_at']):
                    raise AccessError('Начало смены не может быть позже её завершения.', 409)
                await self.confirm(conn, actor, row, body['purpose'], day, when, row[field])
            else:
                await conn.execute(text(f"UPDATE {table} SET status='REJECTED' WHERE id=:id"), {'id': row['id']})
                from .attendance_fines import reconcile_day
                await reconcile_day(self.api, conn, uid, day, self.now())
            # Superseded pending/decision alerts must not be delivered after
            # a newer review result; the audit trail retains every decision.
            await conn.execute(text('''UPDATE notifications SET resolved_at=:now
                WHERE kind='attendance' AND event_key LIKE :prefix AND resolved_at IS NULL'''),
                {'now': self.now().replace(tzinfo=None),
                 'prefix': f'attendance:{body["purpose"]}:{row["id"]}:%'})
            await self.notify_submission(conn, actor, body['purpose'], row['id'], day, worker_id=uid,
                decision={'corrected': correcting, 'note': body['note'].strip()})
            await self.api.audit_write(conn, actor, 'offline_attendance_reviewed', table, row['id'],
                json.dumps({'before': row['status'], 'previousAt': utc(row[at]).isoformat() if row.get(at) else None,
                            'after': final if when else 'REJECTED',
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
    from .attendance_fines import install_attendance_fines
    install_attendance_fines(app, miniapp)
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
