"""Photo correction, separate from Academy. AI only selects bounded color parameters."""
import asyncio
import base64
import hashlib
import io
import json
import math
from datetime import timedelta

from aiohttp import ClientError, web
from PIL import Image, ImageEnhance, ImageOps
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .db import Session
from .miniapp_security import AccessError
from .models import Booking, PhotoEdit, PhotoStorage, Shooting, utc_now
from .services.development_ai import structured
from .yandex_disk import ROOT, YandexDiskError

LIMITS = {'exposure': (-1.5, 1.5), 'contrast': (0.8, 1.2), 'color': (0.8, 1.2),
          'red': (0.85, 1.15), 'blue': (0.85, 1.15)}
DEFAULTS = {'exposure': 0, 'contrast': 1, 'color': 1, 'red': 1, 'blue': 1}


def parameters(value):
    if not isinstance(value, dict) or set(value) != set(LIMITS):
        raise AccessError('Нужны настройки света, контраста, цвета и баланса белого.', 400)
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or not lo <= value[k] <= hi
           for k, (lo, hi) in LIMITS.items()):
        raise AccessError('Настройки обработки вне допустимых границ.', 400)
    return value


def correct(raw, settings):
    settings = parameters(settings)
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError('Изображение больше 40 мегапикселей.')
        image = ImageOps.exif_transpose(source).convert('RGB')
        gain = 2 ** settings['exposure']
        bands = image.split()
        image = Image.merge('RGB', tuple(band.point([min(255, round(v * gain * multiplier)) for v in range(256)])
                                        for band, multiplier in zip(bands, (settings['red'], 1, settings['blue']))))
        image = ImageEnhance.Contrast(image).enhance(settings['contrast'])
        image = ImageEnhance.Color(image).enhance(settings['color'])
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=95, icc_profile=source.info.get('icc_profile'))
        return output.getvalue()


async def access(session, actor, sid):
    shooting = await session.get(Shooting, sid)
    booking = await session.get(Booking, shooting.booking_id) if shooting else None
    roles = set(actor['roles'])
    if booking is None or not (roles & {'OWNER', 'ADMIN'} or
                              ('PHOTOGRAPHER' in roles and booking.photographer_id == actor['id'])):
        raise AccessError('Нет доступа к фотографиям этой съёмки.', 403)
    return shooting


async def apply(session, actor, data):
    if set(data) != {'shooting', 'photo', 'action', 'edit', 'parameters'} or type(data['shooting']) is not int or type(data['photo']) is not int:
        raise AccessError('Некорректная операция обработки.', 400)
    await access(session, actor, data['shooting'])
    original = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == data['photo'], PhotoStorage.shooting_id == data['shooting']))
    if original is None:
        raise AccessError('Фотография не найдена в этой съёмке.', 404)
    if data['action'] in {'cancel', 'retry'}:
        if type(data['edit']) is not int:
            raise AccessError('Не указана версия.', 400)
        edit = await session.get(PhotoEdit, data['edit'], with_for_update=True)
        if edit is None or edit.photo_id != original.photo_id:
            raise AccessError('Версия не найдена.', 404)
        if data['action'] == 'cancel':
            edit.status = 'CANCELLED'
        elif edit.status == 'FAILED':
            edit.status, edit.attempts, edit.last_error = 'PENDING', 0, None
        else:
            raise AccessError('Повтор доступен только для неудачной обработки.', 409)
    else:
        if data['action'] not in {'MANUAL', 'AI'} or original.status != 'STORED':
            raise AccessError('Сначала дождитесь сохранения оригинала на Диске.', 409)
        if data['action'] == 'AI' and not config.openai_api_key:
            raise AccessError('AI не подключён. Ручная обработка доступна.', 409)
        settings = parameters(data['parameters']) if data['action'] == 'MANUAL' else None
        edit = PhotoEdit(photo_id=original.photo_id, created_by_id=actor['id'], mode=data['action'],
                         parameters=json.dumps(settings) if settings else None)
        session.add(edit)
        await session.flush()
    return {'photoId': original.photo_id, 'editId': edit.id, 'status': edit.status}


async def process_one(storage):
    now = utc_now()
    async with Session() as session:
        await session.execute(update(PhotoEdit).where(PhotoEdit.status == 'RUNNING', PhotoEdit.claimed_at < now - timedelta(minutes=10))
                              .values(status='FAILED', last_error='Обработка прервана. Повторите.'))
        edit = await session.scalar(select(PhotoEdit).where(PhotoEdit.status == 'PENDING').order_by(PhotoEdit.id).with_for_update(skip_locked=True).limit(1))
        if edit is None:
            await session.commit()
            return False
        original = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == edit.photo_id, PhotoStorage.status == 'STORED'))
        edit.status, edit.claimed_at, edit.attempts = 'RUNNING', now, edit.attempts + 1
        eid, attempt, mode, saved = edit.id, edit.attempts, edit.mode, edit.parameters
        source_path, sid, digest = (original.disk_path, original.shooting_id, original.sha256) if original else (None, None, None)
        await session.commit()
    try:
        if not source_path:
            raise ValueError('Оригинал ещё не сохранён.')
        raw = await storage.download_bytes(source_path)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Оригинал не прошёл проверку целостности.')
        settings = json.loads(saved) if saved else None
        if mode == 'AI' and settings is None:
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 40_000_000:
                    raise ValueError('Изображение больше 40 мегапикселей.')
                preview = ImageOps.exif_transpose(source).convert('RGB')
                preview.thumbnail((1200, 1200))
                buffer = io.BytesIO()
                preview.save(buffer, format='JPEG')
            schema = {'type':'object', 'properties':{k:{'type':'number','minimum':lo,'maximum':hi} for k,(lo,hi) in LIMITS.items()},
                      'required':list(LIMITS), 'additionalProperties':False}
            result = await structured('Подбери мягкую естественную цветокоррекцию в едином стиле Photo Boss: нейтральный баланс белого, естественная кожа, умеренный контраст. Верни только числовые параметры: exposure в ступенях EV, остальные множители. Не меняй личность, лицо, возраст, телосложение; не выполняй ретушь или генерацию.',
                [{'type':'input_image','image_url':'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()}], schema, 'photo_correction', limit=500)
            if result['status'] != 'completed':
                raise ValueError('AI недоступен. Используйте ручную обработку.')
            settings = parameters(result['data'])
        output = await asyncio.to_thread(correct, raw, settings)
        if len(output) > 20 * 1024 * 1024:
            raise ValueError('Обработанная копия больше 20 МБ.')
        folder = ROOT + f'/shootings/{sid}/processed'
        await storage.ensure_dir(folder)
        path = folder + f'/edit-{eid}-{hashlib.sha256(output).hexdigest()}.jpg'
        await storage.upload_bytes(path, output, content_type='image/jpeg')
        async with Session() as session:
            await session.execute(update(PhotoEdit).where(PhotoEdit.id == eid, PhotoEdit.status == 'RUNNING', PhotoEdit.attempts == attempt, PhotoEdit.claimed_at == now)
                                  .values(status='READY', disk_path=path, parameters=json.dumps(settings), last_error=None))
            await session.commit()
    except (ClientError, TimeoutError, OSError, ValueError, AccessError, YandexDiskError):
        async with Session() as session:
            await session.execute(update(PhotoEdit).where(PhotoEdit.id == eid, PhotoEdit.status == 'RUNNING', PhotoEdit.attempts == attempt, PhotoEdit.claimed_at == now)
                                  .values(status='FAILED', last_error='Обработка не завершена. Оригинал сохранён. Повторите или используйте ручные настройки.'))
            await session.commit()
    return True


class PhotoFiles:
    def __init__(self, api):
        self.api = api

    async def image(self, request):
        try:
            pid = int(request.match_info['photo'])
            eid = int(request.query.get('edit', '0'))
        except ValueError:
            raise AccessError('Некорректный номер кадра.', 400) from None
        async with AsyncSession(self.api.engine) as session:
            original = await session.scalar(select(PhotoStorage).where(PhotoStorage.photo_id == pid))
            if original is None:
                raise AccessError('Фотография не найдена.', 404)
            await access(session, request['miniapp_actor'], original.shooting_id)
            path = original.disk_path if original.status == 'STORED' else None
            if eid:
                edit = await session.get(PhotoEdit, eid)
                path = edit.disk_path if edit and edit.photo_id == pid and edit.status == 'READY' else None
            if not path:
                raise AccessError('Файл пока недоступен.', 409)
        raw = await request.app['yandex_disk'].download_bytes(path)
        from .services.photo_storage import image_format
        return web.Response(body=raw, content_type=image_format(raw)[1], headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})
