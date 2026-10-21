"""Real camera UI with isolated attendance policy fixtures.

Disposable users and SQLite only. Chromium supplies a synthetic camera stream;
external image storage is mocked. No real GPS, push, or employee data is used.
"""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_services import fixture

from app.attendance import Attendance, install_attendance
from app.hotel_points import context_key
from app.miniapp_api import install_miniapp
from app.models import (
    Notification,
    Setting,
    ShiftCheckIn,
    ShiftCheckOut,
    User,
    UserRole,
)
from app.people import install_people
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat


async def make_fixture(directory):
    engine, factory = await fixture('sqlite+aiosqlite:///' + str(directory / 'attendance.db'))
    async with factory() as session:
        session.add_all([User(id=3, tg_id=3, name='Fixture owner'), UserRole(user_id=3, role='OWNER')])
        await session.commit()
    bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
        me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app = web.Application(client_max_size=2 * 1024 * 1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    original_rows = api.rows

    async def rows(conn, sql, **params):
        # Only the disposable SQLite fixture lacks PostgreSQL's row-lock syntax.
        return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

    api.rows = rows
    install_attendance(app, api)
    install_people(app, api)
    install_work_calls(app, install_work_chat(app, api))
    return app, engine, factory, bot


async def main(*, gps_only=False):
    settings = {'PHOTO_BOSS_ATTENDANCE_GPS_REQUIRED': '1' if gps_only else '0',
                'PHOTO_BOSS_ATTENDANCE_GEOFENCE_REQUIRED': '0' if gps_only else '1',
                'PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW': '1',
                'PHOTO_BOSS_NOTIFICATION_CHANNEL': 'app'}
    with tempfile.TemporaryDirectory(prefix='photo-boss-photo-shift-') as folder, patch.dict(os.environ, settings):
        root = Path(folder)
        app, engine, factory, bot = await make_fixture(root)
        if gps_only:
            # Recover a complete old online upload that waited for an unconfigured
            # hotel zone. Its timestamp is server evidence, not a phone claim.
            now = datetime.now(UTC)
            earlier = (now - timedelta(seconds=1)).replace(tzinfo=None)
            async with factory() as session:
                start = ShiftCheckIn(user_id=1, shift_date=now.astimezone(ZoneInfo('Europe/Moscow')).date(),
                    status='PENDING_REVIEW', latitude=44.9, longitude=37.3,
                    location_received_at=earlier, full_body_file_id='app:/fixture/old-pending.jpg',
                    offline_claimed_at=earlier)
                session.add(start)
                await session.flush()
                session.add(Setting(key=context_key('start', start.id), value=json.dumps({
                    'source': 'online', 'state': 'not_configured', 'hotelId': 1,
                    'hotelName': 'Fixture hotel', 'accuracyM': 10})))
                await session.commit()
        cert, key = root / 'cert.pem', root / 'key.pem'
        await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server = TestServer(app)
        await server.start_server(ssl=tls)
        images = AsyncMock(return_value='app:/fixture/attendance-camera.jpg')
        errors, location_requests, photos = [], [], []
        evidence = Path(os.getenv('CALLS_QA_DIR', '/tmp/photo-boss-attendance-qa'))
        evidence.mkdir(parents=True, exist_ok=True)
        evidence_prefix = 'attendance-gps-only' if gps_only else 'attendance-photo-only'
        try:
            with patch.object(Attendance, 'send_photo', images):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None,
                        args=['--ignore-certificate-errors', '--use-fake-device-for-media-stream',
                              '--use-fake-ui-for-media-stream'])
                    try:
                        context = await browser.new_context(ignore_https_errors=True,
                            viewport={'width': 390, 'height': 844}, is_mobile=True,
                            has_touch=True, permissions=['camera', 'geolocation'] if gps_only else ['camera'],
                            **({'geolocation': {'latitude': 44.9, 'longitude': 37.3}} if gps_only else {}))
                        gps_script = ('const fixtureGetPosition=navigator.geolocation.getCurrentPosition.bind(navigator.geolocation);'
                            'navigator.geolocation.getCurrentPosition=(...args)=>{window.fixtureGpsRequests++;return fixtureGetPosition(...args);};'
                            if gps_only else 'navigator.geolocation.getCurrentPosition=()=>{window.fixtureGpsRequests++;'
                            'throw new Error("GPS must not be requested in the temporary photo-only policy");};')
                        await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(1)) + '}};'
                            'window.fixtureGpsRequests=0;' + gps_script)
                        await context.route('https://telegram.org/**', lambda route: route.fulfill(
                            body='', content_type='text/javascript'))
                        page = await context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))

                        def observe(request):
                            if request.url.endswith('/attendance/location'):
                                location_requests.append(request.url)
                            if request.url.endswith('/attendance/photo') and request.method == 'POST':
                                photos.append(request.post_data_json)

                        page.on('request', observe)
                        await page.goto(str(server.make_url('/app/#home')))
                        await page.locator('#attendanceCard button').click()
                        dialog = page.locator('#attendanceDialog')
                        if gps_only:
                            await expect(dialog).to_contain_text('Геолокация обязательна')
                            await expect(dialog).to_contain_text('Проверка расстояния до отеля временно отключена')
                            await dialog.locator('[data-att=retake-start]').click()
                        else:
                            await expect(dialog).to_contain_text('Геолокация временно отключена')
                            assert await dialog.locator('[data-att=location]').count() == 0

                        async def capture_and_confirm(purpose):
                            if gps_only:
                                await expect(dialog.locator('[data-att=camera]')).to_be_disabled()
                                async with page.expect_response(lambda response: response.url.endswith('/attendance/location')
                                        and response.request.method == 'POST' and response.status == 200):
                                    await dialog.locator('[data-att=location]').click()
                                await expect(dialog).to_contain_text('Координаты сохранены на сервере')
                            await expect(dialog.locator('[data-att=camera]')).to_be_enabled()
                            await dialog.locator('[data-att=camera]').click()
                            await page.wait_for_function('document.querySelector("#attCameraVideo")?.videoWidth>=96')
                            await dialog.locator('[data-att=snap]').click()
                            await expect(dialog.locator('[data-att=confirm]')).to_be_enabled()
                            async with page.expect_response(lambda response: response.url.endswith('/attendance/photo')
                                    and response.request.method == 'POST' and response.status == 200) as response:
                                await dialog.locator('[data-att=confirm]').click()
                            result = await (await response.value).json()
                            assert photos[-1]['purpose'] == purpose
                            assert set(photos[-1]) == {'purpose', 'date', 'image'}
                            assert result['gpsRequired'] is gps_only
                            if gps_only:
                                assert result['geofenceRequired'] is False and result['geoPolicy'] == 'gps_only'
                            return result

                        started = await capture_and_confirm('start')
                        assert started['start']['status'] == 'STARTED'
                        assert started['start']['locationSaved'] is gps_only
                        await expect(dialog).to_contain_text('Вы на смене')
                        await page.screenshot(path=str(evidence / (evidence_prefix + '-started.png')), full_page=True)
                        retry_start = await page.evaluate("""async body=>{
                            const {api}=await import('/app/js/api.js');
                            return api('/attendance/photo',{method:'POST',body});
                        }""", photos[-1])
                        assert retry_start['start']['status'] == 'STARTED'
                        assert images.await_count == 1, 'A successful shift-start retry must not store a second photo'
                        async with factory() as session:
                            notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                            assert len(notices) == 2 and {row.user_id for row in notices} == {1, 3}
                            assert all(json.loads(row.payload)['status'] == 'STARTED' for row in notices)

                        ended = await capture_and_confirm('end')
                        assert ended['end']['status'] == 'FINISHED'
                        assert ended['end']['locationSaved'] is gps_only
                        await expect(dialog).not_to_be_visible()
                        retry_end = await page.evaluate("""async body=>{
                            const {api}=await import('/app/js/api.js');
                            return api('/attendance/photo',{method:'POST',body});
                        }""", photos[-1])
                        assert retry_end['end']['status'] == 'FINISHED'
                        assert images.await_count == 2
                        async with factory() as session:
                            assert await session.scalar(select(func.count(ShiftCheckIn.id))) == 1
                            assert await session.scalar(select(func.count(ShiftCheckOut.id))) == 1
                            start = await session.scalar(select(ShiftCheckIn))
                            end = await session.scalar(select(ShiftCheckOut))
                            assert start.started_at and end.ended_at and end.ended_at >= start.started_at
                            if gps_only:
                                assert (start.latitude, start.longitude) == (44.9, 37.3)
                                assert (end.latitude, end.longitude) == (44.9, 37.3)
                            else:
                                assert start.latitude is None and start.longitude is None
                                assert end.latitude is None and end.longitude is None
                            notices = list(await session.scalars(select(Notification).where(Notification.kind == 'attendance')))
                            assert len(notices) == 4
                            assert len([row for row in notices if row.user_id == 3]) == 2
                            assert {json.loads(row.payload)['status'] for row in notices} == {'STARTED', 'FINISHED'}
                        gps_count = await page.evaluate('window.fixtureGpsRequests')
                        await page.goto(str(server.make_url('/app/#home')))
                        await page.locator('#attendanceCard button').click()
                        await expect(dialog).to_contain_text('Смена завершена')
                        await expect(dialog).to_contain_text('Геолокации и фотографии сохранены' if gps_only else 'Фотографии и время сохранены')
                        assert len(location_requests) == (2 if gps_only else 0)
                        assert gps_count == (2 if gps_only else 0)
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        assert not errors, errors
                        assert bot.send_message.await_count == 0, 'Fixture must not send real messages'
                        await page.screenshot(path=str(evidence / (evidence_prefix + '-finished.png')), full_page=True)
                        print('PASS: mobile ' + ('mandatory GPS with deferred hotel zones, pending start recovery, ' if gps_only else 'camera-only shift, no GPS requests, ') +
                              'start/end, leadership notifications, retry idempotence, two stored photos and one shift ledger')
                    finally:
                        await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
