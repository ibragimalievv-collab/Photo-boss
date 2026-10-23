"""Disposable, public QA deployment. Never imports production startup hooks.

The business HTTP handlers and frontend are unmodified. SQLite, synthetic
identities, camera/GPS adapters and local file storage are explicit test
boundaries, not evidence of real Telegram/Disk/device delivery.
"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

QA_TOKEN = '123456:ISOLATED_QA_NO_TELEGRAM_ACCESS'
if (os.getenv('PHOTO_BOSS_QA_SERVER') != '1' or os.getenv('BOT_TOKEN') != QA_TOKEN
        or os.getenv('DATABASE_URL') != 'sqlite+aiosqlite:////tmp/photo-boss-isolated-qa/qa.db'):
    raise RuntimeError('QA requires explicit mode, synthetic token and SQLite only')
if any(os.getenv(k) for k in ('YANDEX_DISK_TOKEN', 'OPENAI_API_KEY', 'SBER_MERCHANT_PASSWORD',
                              'HR_BOT_TOKEN', 'AVITO_CLIENT_SECRET', 'WEB_PUSH_PRIVATE_KEY')):
    raise RuntimeError('Real integration secrets are forbidden on QA')

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.academy_practice import install_academy_practice
from app.attendance import install_attendance
from app.db import Base
from app.miniapp_api import install_miniapp
from app.models import (
    Hotel,
    HotelEmployee,
    Package,
    Shift,
    User,
    UserRole,
)
from app.people import install_people
from app.services import media_files
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat
from app.workflow import Workflow
from app.yandex_disk import YandexDiskError

ROOT = Path('/tmp/photo-boss-isolated-qa')
ROLES = {1: 'Менеджер', 2: 'Фотограф', 3: 'Владелец', 4: 'Администратор'}


def signed(uid):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid})}
    key = hmac.new(b'WebAppData', QA_TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class LocalFiles:
    def file(self, path):
        return ROOT / 'files' / hashlib.sha256(path.encode()).hexdigest()

    async def ensure_dir(self, path):
        (ROOT/'files').mkdir(parents=True, exist_ok=True)

    async def upload_bytes(self, path, raw, **kwargs):
        await self.ensure_dir(path)
        self.file(path).write_bytes(raw)
        if hashlib.sha256(self.file(path).read_bytes()).digest() != hashlib.sha256(raw).digest():
            raise RuntimeError('QA local checksum mismatch')

    async def download_bytes(self, path, max_bytes=20*1024*1024, **kwargs):
        try:
            raw = self.file(path).read_bytes()
        except FileNotFoundError:
            raise YandexDiskError('QA file absent', http_status=404) from None
        if len(raw) > max_bytes:
            raise YandexDiskError('QA file too large')
        return raw

    async def metadata(self, path):
        raw = await self.download_bytes(path)
        return {'path': path, 'type': 'file', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def public_status(self):
        return {'provider': 'QA_LOCAL_NOT_YANDEX', 'configured': True, 'connected': True,
                'readVerified': True, 'writeVerified': True, 'root': 'QA local files'}


def qa_script(uid):
    return '''<script>
window.Telegram={WebApp:{initData:INIT_DATA,ready(){},expand(){},isVersionAtLeast(){return false;}}};
// Explicit QA device adapters: never installed in the production entry point.
const qaCanvas=document.createElement('canvas');qaCanvas.width=640;qaCanvas.height=800;
const qaDraw=()=>{const c=qaCanvas.getContext('2d');c.fillStyle='#243c61';c.fillRect(0,0,640,800);c.fillStyle='white';c.font='28px sans-serif';c.fillText('SYNTHETIC QA CAMERA',35,200);c.fillText(new Date().toISOString(),35,260);};
qaDraw();setInterval(qaDraw,200);
Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async constraints=>{if(constraints.audio)throw Error('QA: real microphone not connected');return qaCanvas.captureStream(5);},enumerateDevices:async()=>[]},configurable:true});
Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition(ok){ok({coords:{latitude:44.9,longitude:37.3,accuracy:8},timestamp:Date.now()});}},configurable:true});
addEventListener('DOMContentLoaded',()=>{const bar=document.createElement('aside');bar.style='position:fixed;z-index:100000;bottom:0;left:0;right:0;background:#ffe68c;color:#111;padding:8px;font:13px sans-serif';bar.innerHTML='ИЗОЛИРОВАННЫЙ ТЕСТ · синтетические данные, камера/GPS, локальные файлы · внешние отправки отключены · '+ROLE_LINKS;document.body.append(bar);});
</script>'''.replace('INIT_DATA', json.dumps(signed(uid))).replace('ROLE_LINKS', json.dumps(' '.join(
        f'<a style="margin:0 7px;color:#111" href="/qa/actor/{i}">{name}</a>' for i, name in ROLES.items())+' <a style="color:#111" href="/qa/photo-failure">Следующее фото: один сбой 503</a>'))


async def build_app():
    ROOT.mkdir(exist_ok=True)
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        if not await session.scalar(select(User.id).limit(1)):
            session.add_all([User(id=i, tg_id=i, name='QA '+name) for i, name in ROLES.items()])
            session.add_all([Hotel(id=1, name='QA Фиолето'), Hotel(id=2, name='QA Мовенпик'),
                             Package(id=1, name='QA 400 рублей', price_per_photo=400)])
            await session.flush()
            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            for uid, role in ((1, 'MANAGER'), (2, 'PHOTOGRAPHER'), (3, 'OWNER'), (4, 'ADMIN')):
                session.add_all([UserRole(user_id=uid, role=role), HotelEmployee(user_id=uid, hotel_id=2)])
                if uid == 1:
                    session.add(HotelEmployee(user_id=uid, hotel_id=1))
                if uid in (1, 2):
                    session.add(Shift(user_id=uid, hotel_id=2, start_at=today,
                        end_at=today+timedelta(hours=23), status='PLANNED'))
            await session.commit()
    files = LocalFiles()
    media_files.configured_storage = lambda: files
    async def save_original(self, folder, raw):
        path = 'app:/PhotoBoss'+folder+'/'+hashlib.sha256(raw).hexdigest()+'.jpg'
        await files.upload_bytes(path, raw)
        return path
    Workflow.save_disk_original = save_original

    async def forbidden_send(*args, **kwargs):
        raise RuntimeError('External messages disabled in isolated QA')
    bot = SimpleNamespace(token=QA_TOKEN, send_message=forbidden_send,
        me=lambda: asyncio.sleep(0, result=SimpleNamespace(username='isolated_qa_not_a_real_bot')))

    @web.middleware
    async def boundary(request, handler):
        uid = int(request.cookies.get('pb_qa_actor', '1'))
        if uid not in ROLES:
            uid = 1
        if request.path == '/':
            return web.Response(text='Photo Boss isolated QA: no real employee data or integrations')
        if request.path == '/qa/photo-failure':
            response = web.HTTPFound('/app/#home')
            response.set_cookie('pb_qa_photo_failure', '1', httponly=True, samesite='Strict')
            return response
        if request.path.startswith('/qa/actor/'):
            target = int(request.path.rsplit('/', 1)[1])
            if target not in ROLES:
                raise web.HTTPNotFound()
            response = web.HTTPFound('/app/#home')
            response.set_cookie('pb_qa_actor', str(target), httponly=True, samesite='Strict')
            return response
        if request.path == '/app/':
            html = Path('app/webapp/index.html').read_text().replace(
                '<script src="https://telegram.org/js/telegram-web-app.js"></script>', qa_script(uid))
            return web.Response(text=html, content_type='text/html', headers={'Cache-Control':'no-store'})
        if request.path.startswith('/api/miniapp/'):
            if request.path == '/api/miniapp/attendance/photo' and request.cookies.get('pb_qa_photo_failure') == '1':
                response = web.json_response({'error': 'QA: one injected temporary photo upload failure'}, status=503)
                response.del_cookie('pb_qa_photo_failure')
                return response
            headers = dict(request.headers)
            headers['X-Telegram-Init-Data'] = signed(uid)
            request = request.clone(headers=headers)
        return await handler(request)

    app = web.Application(middlewares=[boundary], client_max_size=25*1024*1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    original_rows = api.rows
    async def rows(conn, sql, **params):
        return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    install_attendance(app, api)
    install_people(app, api)
    install_academy_practice(app, api)
    install_work_calls(app, install_work_chat(app, api))
    app['yandex_disk'] = files
    app.router.add_get('/', lambda request: web.Response(text='Isolated QA'))
    app.router.add_get('/qa/actor/{id}', lambda request: web.Response(text='QA selector'))
    app.router.add_get('/qa/photo-failure', lambda request: web.Response(text='QA failure selector'))
    app.router.add_get('/api/miniapp/storage/status', lambda request: web.json_response(files.public_status()))
    async def startup(app):
        from app.services.booking_reminders import booking_reminder_loop
        app['qa_reminder_task'] = asyncio.create_task(booking_reminder_loop(engine))
    async def cleanup(app):
        app['qa_reminder_task'].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app['qa_reminder_task']
        await engine.dispose()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    web.run_app(build_app(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
