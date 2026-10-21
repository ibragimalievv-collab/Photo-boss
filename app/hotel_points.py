"""Hotel reference points and conservative, reported-location classification.

Client GPS is untrusted evidence. A circle is an initial configurable zone, not
a surveyed hotel boundary; even an inside result cannot confirm attendance.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import unicodedata

from aiohttp import web
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .miniapp_security import AccessError, utc_bounds
from .models import Hotel, Setting, User
from .services.core import audit, roles_of
from .work_breaks import hotel_access

logger = logging.getLogger(__name__)
SEED_ENV = 'PHOTO_BOSS_HOTEL_POINT_SEED'
REVIEW_ENV = 'PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW'


def review_required():
    return os.getenv(REVIEW_ENV, '0').strip() == '1'


def point_key(hotel_id):
    return f'attendance:hotel:{hotel_id}:point'


def context_key(purpose, row_id):
    return f'attendance:geo:{purpose}:{row_id}'


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def point_payload(value):
    if not isinstance(value, dict) or set(value) != {'address', 'latitude', 'longitude', 'radiusM'}:
        raise AccessError('Проверьте адрес, координаты и радиус отеля.', 400)
    address = value['address']
    if not isinstance(address, str) or not 3 <= len(address.strip()) <= 300 or any(ord(c) < 32 for c in address):
        raise AccessError('Укажите адрес отеля.', 400)
    for key, lo, hi in [('latitude', -90, 90), ('longitude', -180, 180), ('radiusM', 20, 1000)]:
        number = value[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not lo <= number <= hi:
            raise AccessError('Некорректные координаты или радиус. Допустимый радиус: 20–1000 м.', 400)
    return value | {'address': address.strip()}


def classify(point, latitude, longitude, accuracy):
    lat1, lat2 = math.radians(point['latitude']), math.radians(latitude)
    dlat, dlon = lat2-lat1, math.radians(longitude-point['longitude'])
    hav = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    distance = 6371008.8 * 2 * math.asin(math.sqrt(min(1, max(0, hav))))
    # Missing/zero/very poor device accuracy cannot justify an inside result.
    precise = accuracy is not None and 0 < accuracy <= min(100, point['radiusM']/2)
    state = ('inside' if distance+accuracy <= point['radiusM'] else
             'outside' if distance-accuracy > point['radiusM'] else 'uncertain') if precise else 'uncertain'
    return {'state': state, 'distanceM': round(distance, 1), 'accuracyM': accuracy,
            'radiusM': point['radiusM'], 'pointRevision': revision(point),
            'notice': {'inside': 'Координаты устройства в пределах зоны; присутствие ещё не подтверждено.',
                       'outside': 'Координаты устройства за пределами зоны. Нужна проверка руководителя.',
                       'uncertain': 'Погрешность координат не позволяет уверенно проверить зону.'}[state]}


async def read_json(api, conn, key):
    rows = await api.rows(conn, 'SELECT value FROM settings WHERE key=:key', key=key)
    if not rows:
        return None
    try:
        value = json.loads(rows[0]['value'])
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


async def save_json(conn, key, value):
    await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) '
        'ON CONFLICT (key) DO UPDATE SET value=excluded.value'),
        {'key': key, 'value': json.dumps(value, ensure_ascii=False)})


async def capture_context(api, conn, user_id, day, purpose, row_id, latitude, longitude, accuracy, *, source='online', gps_disabled=False):
    lo, hi = utc_bounds(day, day, api.tz)
    hotels = await api.rows(conn, '''SELECT DISTINCT h.id,h.name FROM shifts s JOIN hotels h ON h.id=s.hotel_id
        WHERE s.user_id=:uid AND s.status<>'CANCELLED' AND s.start_at<:hi AND s.end_at>:lo AND h.active=TRUE''',
        uid=user_id, lo=lo, hi=hi)
    basis = 'schedule'
    if not hotels:
        basis = 'membership'
        hotels = await api.rows(conn, '''SELECT DISTINCT h.id,h.name FROM hotel_employees he
            JOIN hotels h ON h.id=he.hotel_id WHERE he.user_id=:uid AND h.active=TRUE''', uid=user_id)
    value = {'state': 'unconfigured', 'source': source, 'accuracyM': accuracy,
             'notice': 'Нет однозначной зоны отеля. Проверяет руководитель.', 'hotelBasis': basis}
    # Never silently choose the nearest hotel: that would let the claimant
    # decide which hotel's attendance is being recorded.
    if len(hotels) == 1:
        hotel = hotels[0]
        value.update(hotelId=hotel['id'], hotelName=hotel['name'])
        point = await read_json(api, conn, point_key(hotel['id']))
        if point:
            try:
                point = point_payload(point)
            except AccessError:
                point = None
        if point and not gps_disabled:
            value.update(classify(point, latitude, longitude, accuracy), address=point['address'])
    elif len(hotels) > 1:
        value.update(state='ambiguous', notice='На этот день доступно несколько отелей. Проверяет руководитель.')
    if gps_disabled:
        value.update(state='temporarily_disabled', gpsRequired=False,
            notice='GPS временно отключён по решению владельца. Местоположение не проверялось.')
    await save_json(conn, context_key(purpose, row_id), value)
    return value


def normalized_name(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).lower() if c.isalnum())


async def startup_hotel_point(engine, raw, admin_ids):
    if not raw.strip():
        return None
    seed = json.loads(raw)
    if not isinstance(seed, dict) or set(seed) != {'requestId', 'match', 'point'} or seed['match'] != 'movenpick':
        raise RuntimeError('Invalid hotel point seed')
    if not isinstance(seed['requestId'], str) or not 1 <= len(seed['requestId']) <= 60:
        raise RuntimeError('Invalid hotel point request ID')
    point = point_payload(seed['point'])
    async with AsyncSession(engine, expire_on_commit=False) as session:
        hotels = list(await session.scalars(select(Hotel).where(Hotel.active.is_(True)).with_for_update()))
        matches = [h for h in hotels if any(alias in normalized_name(h.name)
            for alias in ('movenpick', 'мовенпик', 'мовемпик', 'мевенпик', 'мевемпик'))]
        if len(matches) != 1:
            logger.error('Hotel point not bound: Movenpick matches=%s; active hotels=%s', len(matches), [h.name for h in hotels])
            return {'bound': False}
        hotel = matches[0]
        key = f'hotel-point-seed:{seed["requestId"]}'
        previous = await session.get(Setting, key)
        if previous:
            if previous.value != revision(seed):
                raise RuntimeError('Hotel point seed ID reused with different data')
            stored = await session.get(Setting, point_key(hotel.id))
            if not stored:
                raise RuntimeError('Seeded hotel point missing')
            point = point_payload(json.loads(stored.value))
        else:
            stored = await session.get(Setting, point_key(hotel.id))
            if stored:
                raise RuntimeError('Hotel point already exists; use the authenticated editor')
            session.add(Setting(key=point_key(hotel.id), value=json.dumps(point, ensure_ascii=False)))
            session.add(Setting(key=key, value=revision(seed)))
            hotel.address = point['address']
            actor = await session.scalar(select(User).where(User.tg_id.in_(admin_ids)).limit(1))
            if not actor or not actor.active or 'OWNER' not in await roles_of(session, actor):
                raise RuntimeError('Hotel point owner not found')
            await audit(session, actor, 'hotel_point_saved', 'hotel', hotel.id, json.dumps(point, ensure_ascii=False))
            await session.commit()
        logger.info('Hotel point verified: hotel_id=%s name=%s address=%s latitude=%s longitude=%s radius_m=%s review_required=%s',
                    hotel.id, hotel.name, hotel.address, point['latitude'], point['longitude'], point['radiusM'], review_required())
        return {'bound': True, 'hotelId': hotel.id}


class HotelPoints:
    def __init__(self, api):
        self.api = api

    async def listing(self, request):
        from .people import check_editor
        actor = request['miniapp_actor']
        check_editor(actor['roles'])
        async with self.api.engine.connect() as conn:
            # Re-read current roles and active state, also for a session opened
            # before an administrator was demoted or disabled.
            users = await self.api.rows(conn, 'SELECT active FROM users WHERE id=:id', id=actor['id'])
            roles = await self.api.rows(conn, 'SELECT role FROM user_roles WHERE user_id=:id', id=actor['id'])
            current = {r['role'] for r in roles} if users and users[0]['active'] else set()
            check_editor(current)
            clause = '' if 'OWNER' in current else 'AND EXISTS (SELECT 1 FROM hotel_employees he WHERE he.hotel_id=h.id AND he.user_id=:uid)'
            hotels = await self.api.rows(conn, f'SELECT h.id,h.name,h.address FROM hotels h WHERE h.active=TRUE {clause} ORDER BY h.name,h.id', uid=actor['id'])
            items = []
            for hotel in hotels:
                point = await read_json(self.api, conn, point_key(hotel['id']))
                items.append(dict(hotel) | {'point': point, 'revision': revision(point)})
        from .attendance import gps_required
        return web.json_response({'items': items, 'reviewRequired': review_required(), 'gpsRequired': gps_required()}, headers={'Cache-Control': 'no-store'})

    async def save(self, request):
        from .people import positive_id
        body = await self.api.body(request)
        if set(body) != {'hotelId', 'revision', 'point'}:
            raise AccessError('Обновите форму метки отеля.', 400)
        hid, point = positive_id(body['hotelId']), point_payload(body['point'])
        async with AsyncSession(self.api.engine, expire_on_commit=False) as session:
            actor = await session.get(User, request['miniapp_actor']['id'], with_for_update=True)
            roles = await roles_of(session, actor) if actor and actor.active else set()
            await hotel_access(session, actor.id if actor else 0, roles, hid, manage=True)
            hotel = await session.get(Hotel, hid, with_for_update=True)
            stored = await session.get(Setting, point_key(hid))
            previous = json.loads(stored.value) if stored else None
            if body['revision'] != revision(previous):
                raise AccessError('Метка уже изменена. Откройте её заново.', 409)
            if stored:
                stored.value = json.dumps(point, ensure_ascii=False)
            else:
                session.add(Setting(key=point_key(hid), value=json.dumps(point, ensure_ascii=False)))
            hotel.address = point['address']
            await audit(session, actor, 'hotel_point_saved', 'hotel', hid,
                        json.dumps({'before': previous, 'after': point}, ensure_ascii=False))
            await session.commit()
        return web.json_response({'ok': True, 'revision': revision(point)})


def install_hotel_points(app, api):
    service = HotelPoints(api)
    app.router.add_get('/api/miniapp/hotel-points', service.listing)
    app.router.add_put('/api/miniapp/hotel-points', service.save)
