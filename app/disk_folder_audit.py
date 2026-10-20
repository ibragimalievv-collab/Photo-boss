"""Owner-requested, read-only audits of private folders outside the app namespace."""
import asyncio
import hashlib
import json
import logging
import os
from datetime import timedelta
from uuid import UUID, uuid4

from aiohttp import web
from PIL import Image
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import config
from .miniapp_security import AccessError, require_owner
from .models import Booking, Setting, User, UserRole, utc_now
from .services.shooting_media_audit import audit_summary, inspect_original
from .yandex_disk import (
    YandexDisk,
    YandexDiskError,
    configured_from_env,
    safe_disk_read_path,
)

PREFIX = 'disk-folder-audit:'
PHOTO_SUFFIXES = frozenset({'jpg', 'jpeg', 'png', 'webp', 'tif', 'tiff', 'heic', 'heif', 'cr2', 'cr3', 'nef', 'arw', 'dng', 'raf'})
MAX_PHOTOS = 1000
MAX_FOLDERS = 200
logger = logging.getLogger(__name__)


def read_storage():
    token, client = configured_from_env()
    return YandexDisk(os.getenv('YANDEX_DISK_READ_TOKEN', '').strip() or token, client)


def source_id(storage):
    return hashlib.sha256(storage.token.encode()).hexdigest()


def provider_error(exc):
    if 'HTTP 403' in str(exc):
        return 'Яндекс не разрешил чтение папки. Подключите аккаунт с разрешением чтения этих файлов.'
    if 'HTTP 401' in str(exc):
        return 'Доступ Яндекс.Диска истёк. Владелец должен заново подключить аккаунт.'
    if 'HTTP 404' in str(exc):
        return 'Папка или файл не найдены на подключённом Диске.'
    return 'Не удалось прочитать Яндекс.Диск. Исходные файлы сохранены; повторите проверку.'


def child_path(parent, item):
    name = item.get('name')
    if not isinstance(name, str) or not name or '/' in name:
        raise ValueError('Яндекс вернул некорректное имя файла.')
    expected = parent.rstrip('/') + '/' + name
    if item.get('path', expected) != expected:
        raise ValueError('Папка изменилась во время чтения. Запустите новую проверку.')
    return safe_disk_read_path(expected)


def public_job(job):
    files = job['files']
    summary = audit_summary(job['results'], len(files))
    summary['listingComplete'] = job['stage'] != 'LISTING'
    summary['complete'] = job['status'] == 'COMPLETED' and summary['checked'] == len(files)
    names = {i + 1: row['name'] for i, row in enumerate(files)}
    paths = {i + 1: row['path'] for i, row in enumerate(files)}
    return {key: job.get(key) for key in ('id', 'path', 'bookingId', 'status', 'stage', 'createdAt', 'updatedAt', 'error', 'otherFiles')} | {
        'totalPhotos': len(files), 'processed': job['offset'], 'summary': summary,
        'files': [row | {'name': names.get(row['photoId'], 'Фото'), 'path': paths.get(row['photoId'])} for row in job['results']],
        'aiFrames': [frame | {'name': names.get(frame['photo_id'], 'Фото'), 'path': paths.get(frame['photo_id'])} for frame in job['aiFrames']],
        'aiErrors': job['aiErrors'], 'aiConfigured': bool(config.openai_api_key),
        'note': 'Проверка читает файлы с Диска. Не загружает копии, не удаляет исходники и не меняет продажи. Похожие кадры и AI-оценки требуют проверки человеком.'}


class DiskFolderAudits:
    def __init__(self, api):
        self.api = api

    @staticmethod
    def owner(request):
        actor = request['miniapp_actor']
        require_owner(actor['roles'])
        return actor

    async def folders(self, request):
        self.owner(request)
        try:
            path = safe_disk_read_path(request.query.get('path', 'disk:/'))
            items = await read_storage().disk_list_directory(path)
            folders = [{'name': item['name'], 'path': child_path(path, item)}
                       for item in items if item.get('type') == 'dir']
        except ValueError as exc:
            raise AccessError(str(exc), 400) from exc
        except YandexDiskError as exc:
            raise AccessError(provider_error(exc), 403 if 'HTTP 403' in str(exc) else 503) from exc
        return web.json_response({'path': path, 'folders': folders, 'filesHere': sum(i.get('type') == 'file' for i in items)},
                                 headers={'Cache-Control': 'no-store'})

    async def start(self, request):
        actor = self.owner(request)
        body = await self.api.body(request)
        try:
            if set(body) != {'path', 'bookingId', 'requestKey'}:
                raise ValueError('Выберите папку и съёмку.')
            path = safe_disk_read_path(body['path'])
            jid = str(UUID(body['requestKey']))
            if jid != body['requestKey'] or (body['bookingId'] is not None and type(body['bookingId']) is not int):
                raise ValueError('Некорректные данные проверки.')
        except (ValueError, TypeError, AttributeError) as exc:
            raise AccessError('Выберите папку и корректную съёмку.', 400) from exc
        storage = read_storage()
        if not storage.token:
            raise AccessError('Владелец должен подключить Яндекс.Диск.', 503)
        async with AsyncSession(self.api.engine) as session:
            await session.get(User, actor['id'], with_for_update=True)
            saved = await session.get(Setting, PREFIX + jid)
            if saved:
                job = json.loads(saved.value)
                if job['path'] != path or job['bookingId'] != body['bookingId'] or job['actorId'] != actor['id']:
                    raise AccessError('Номер проверки уже используется для другой папки.', 409)
                return web.json_response(public_job(job))
            if body['bookingId'] is not None and await session.get(Booking, body['bookingId']) is None:
                raise AccessError('Съёмка не найдена.', 404)
            active = list(await session.scalars(select(Setting).where(Setting.key.like(PREFIX + '%'))))
            if sum(json.loads(row.value)['status'] in {'PENDING', 'RUNNING'} for row in active) >= 3:
                raise AccessError('Уже выполняются три проверки. Дождитесь результата.', 429)
            now = utc_now().isoformat()
            job = {'id': jid, 'actorId': actor['id'], 'bookingId': body['bookingId'], 'path': path,
                   'source': source_id(storage), 'status': 'PENDING', 'stage': 'LISTING', 'todo': [path],
                   'seen': [], 'files': [], 'offset': 0, 'results': [], 'aiFrames': [], 'aiErrors': [],
                   'otherFiles': 0, 'createdAt': now, 'updatedAt': now, 'error': None, 'claimedAt': None}
            session.add(Setting(key=PREFIX + jid, value=json.dumps(job, ensure_ascii=False)))
            await session.commit()
        return web.json_response(public_job(job), status=202)

    async def status(self, request):
        self.owner(request)
        try:
            jid = str(UUID(request.match_info['id']))
        except ValueError as exc:
            raise AccessError('Проверка не найдена.', 404) from exc
        async with AsyncSession(self.api.engine) as session:
            row = await session.get(Setting, PREFIX + jid)
            if row is None:
                raise AccessError('Проверка не найдена.', 404)
            return web.json_response(public_job(json.loads(row.value)), headers={'Cache-Control': 'no-store'})


async def process_disk_audit(engine, *, storage=None, batch_size=3):
    """Advance one persistent manifest/batch; stale claims resume after a restart."""
    storage = storage or read_storage()
    async with AsyncSession(engine) as session:
        candidates = list(await session.scalars(select(Setting).where(Setting.key.like(PREFIX + '%'), or_(
            Setting.value.like('%"status": "PENDING"%'), Setting.value.like('%"status": "RUNNING"%'))).order_by(Setting.key)))
        key = None
        for row in candidates:
            value = row.value
            job = json.loads(value)
            if job['status'] not in {'PENDING', 'RUNNING'}:
                continue
            if job['status'] == 'RUNNING' and job['claimedAt'] and job['claimedAt'] > (utc_now() - timedelta(minutes=4)).isoformat():
                continue
            if job.get('attempts', 0) >= 5:
                job.update(status='FAILED', error='Проверка несколько раз прервалась. Создайте новую проверку папки.',
                           updatedAt=utc_now().isoformat(), claimedAt=None)
                await session.execute(update(Setting).where(Setting.key == row.key, Setting.value == value).values(
                    value=json.dumps(job, ensure_ascii=False)))
                await session.commit()
                return True
            claim = uuid4().hex
            job.update(status='RUNNING', claim=claim, claimedAt=utc_now().isoformat(), attempts=job.get('attempts', 0) + 1)
            claimed_value = json.dumps(job, ensure_ascii=False)
            result = await session.execute(update(Setting).where(Setting.key == row.key, Setting.value == value).values(value=claimed_value))
            if result.rowcount:
                key = row.key
                owner = await session.get(User, job['actorId'])
                owner_allowed = bool(owner and owner.active and await session.scalar(select(UserRole.id).where(
                    UserRole.user_id == owner.id, UserRole.role == 'OWNER').limit(1)))
                await session.commit()
                break
        if key is None:
            return False
    try:
        if not owner_allowed:
            raise ValueError('Доступ владельца изменился. Проверка остановлена.')
        if job['source'] != source_id(storage):
            raise ValueError('Подключённый Диск изменился. Создайте новую проверку выбранной папки.')
        if job['stage'] == 'LISTING':
            folder = job['todo'].pop(0)
            if folder in job['seen']:
                raise ValueError('В списке папок обнаружен повтор. Запустите новую проверку.')
            items = await storage.disk_list_directory(folder)
            job['seen'].append(folder)
            for item in items:
                path = child_path(folder, item)
                if item.get('type') == 'dir':
                    job['todo'].append(path)
                elif item.get('type') == 'file' and item['name'].rsplit('.', 1)[-1].casefold() in PHOTO_SUFFIXES:
                    job['files'].append({'path': path, 'name': item['name'], 'size': item.get('size'),
                                         'sha256': item.get('sha256'), 'md5': item.get('md5')})
                elif item.get('type') == 'file':
                    job['otherFiles'] += 1
                if len(job['seen']) + len(job['todo']) > MAX_FOLDERS or len(job['files']) > MAX_PHOTOS:
                    raise ValueError('Для одной проверки доступны до 1000 фотографий и 200 папок. Выберите меньшую папку; проверка всего каталога не завершена.')
            if not job['todo']:
                job['stage'] = 'ANALYSIS'
                if not job['files']:
                    job['status'] = 'COMPLETED'
        else:
            images = []
            finish = min(job['offset'] + batch_size, len(job['files']))
            for index in range(job['offset'], finish):
                photo = job['files'][index]
                try:
                    raw = await storage.disk_download_bytes(photo['path'])
                    result = await asyncio.to_thread(inspect_original, raw, index + 1, photo['sha256'], photo['size'])
                    if photo['md5'] and hashlib.md5(raw, usedforsecurity=False).hexdigest() != photo['md5']:
                        raise ValueError('Контрольная сумма исходника изменилась. Запустите новую проверку папки.')
                    job['results'].append(result)
                    if config.openai_api_key and raw.startswith((b'\xff\xd8\xff', b'\x89PNG')):
                        images.append((index + 1, raw))
                except (YandexDiskError, ValueError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
                    job['results'].append({'photoId': index + 1, 'status': 'unavailable',
                        'error': provider_error(exc) if isinstance(exc, YandexDiskError) else 'Не удалось прочитать изображение или подтвердить размер и контрольную сумму. Для RAW/HEIC нужен поддерживаемый оригинал JPEG/PNG.'})
            if images:
                from .services.development_ai import analyze_batch
                try:
                    response = await analyze_batch(images)
                    if response.get('status') == 'completed':
                        job['aiFrames'].extend(response['data']['frames'])
                    else:
                        job['aiErrors'].append({'photos': [pid for pid, _ in images], 'error': 'AI-проверка этой группы не завершена.'})
                except (TimeoutError, ValueError, OSError):
                    job['aiErrors'].append({'photos': [pid for pid, _ in images], 'error': 'AI временно недоступен.'})
            job['offset'] = finish
            if finish == len(job['files']):
                job['status'] = 'COMPLETED'
        if job['status'] == 'RUNNING':
            job['status'] = 'PENDING'
    except (YandexDiskError, ValueError, TypeError, KeyError) as exc:
        job['status'] = 'FAILED'
        job['error'] = provider_error(exc) if isinstance(exc, YandexDiskError) else str(exc)
    job.update(updatedAt=utc_now().isoformat(), claimedAt=None, attempts=0)
    async with AsyncSession(engine) as session:
        row = await session.get(Setting, key, with_for_update=True)
        current = json.loads(row.value) if row else None
        if current and current.get('claim') == claim:
            row.value = json.dumps(job, ensure_ascii=False)
            await session.commit()
    return True


async def disk_audit_loop(engine):
    while True:
        try:
            await process_disk_audit(engine)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Disk folder audit worker failed; a stale claim will resume')
        await asyncio.sleep(2)


def install_disk_folder_audits(app, api):
    feature = DiskFolderAudits(api)
    for method, path, handler in (('GET', '/storage/disk-folders', feature.folders),
                                  ('POST', '/storage/disk-audits', feature.start),
                                  ('GET', '/storage/disk-audits/{id}', feature.status)):
        app.router.add_route(method, '/api/miniapp' + path, handler)
