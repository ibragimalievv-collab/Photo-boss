"""Real app/inbox and scoped SQLite API; all people, events and SMS are synthetic."""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select, text
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.miniapp_api import install_miniapp
from app.models import Booking, HotelEmployee, Notification, User, UserRole, utc_now
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS

BASE = 'https://notification-center.invalid'


async def main():
    output = Path(os.getenv('NOTIFICATION_QA_DIR', '/tmp/photo-boss-notification-center'))
    output.mkdir(parents=True, exist_ok=True)
    width = int(os.getenv('NOTIFICATION_VIEW_WIDTH', '360'))
    with tempfile.TemporaryDirectory(prefix='pb-inbox-') as directory, patch.dict(os.environ, {'PHOTO_BOSS_NOTIFICATION_CHANNEL': 'app'}):
        engine, sessions, _ = await fixture('sqlite+aiosqlite:///' + str(Path(directory)/'fixture.db'))
        async with engine.connect() as conn:
            await conn.execute(text('PRAGMA journal_mode=WAL'))
            await conn.commit()
        async with sessions() as session:
            session.add_all([User(id=3, tg_id=3, name='Fixture Admin'), User(id=4, tg_id=4, name='Fixture Manager'),
                UserRole(user_id=3, role='ADMIN'), UserRole(user_id=4, role='MANAGER'),
                HotelEmployee(hotel_id=1, user_id=3), HotelEmployee(hotel_id=1, user_id=4)])
            (await session.get(Booking, 1)).shoot_date = datetime.now(ZoneInfo('Europe/Moscow')).date()
            for index in range(40):
                description = 'Два разных события с одинаковым текстом' if index >= 38 else f'Синтетическое рабочее событие {index+1}'
                session.add(Notification(user_id=1, kind='push_test', event_key=f'center:{index}',
                    text=description, payload=json.dumps({'description': description}),
                    priority='critical' if index == 37 else 'info', created_at=utc_now()-timedelta(minutes=40-index)))
            await session.commit()
        app = web.Application()
        bot = SimpleNamespace(token=TOKEN, me=AsyncMock(), send_message=AsyncMock())
        api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
        raw_rows = api.rows

        async def rows(conn, sql, **params):
            return await raw_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
        api.rows = rows
        client = TestClient(TestServer(app))
        await client.start_server()
        errors, read_all_payloads, sms_writes = [], [], []
        injected, fail_read = False, False
        sms = {'available': False, 'enabled': False, 'consent': False, 'canEdit': True,
               'leadMinutes': 60, 'timezone': 'Europe/Moscow', 'status': 'unavailable', 'reason': 'provider_not_configured'}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                try:
                    for uid, role in [(1, 'OWNER'), (3, 'ADMIN'), (4, 'MANAGER')]:
                        context = await browser.new_context(viewport={'width': width, 'height': 800}, is_mobile=width < 768, has_touch=width < 768)
                        init = signed(uid)
                        await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(init)+',ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')

                        def make_route(bound_init):
                            async def route(r):
                                nonlocal injected, fail_read
                                url, method = r.request.url.removeprefix(BASE), r.request.method
                                if r.request.url.startswith('https://telegram.org/'):
                                    return await r.fulfill(body='', content_type='text/javascript')
                                if url.endswith('/sms-reminder'):
                                    if method == 'PUT':
                                        sms_writes.append(r.request.post_data_json)
                                        sms.update(r.request.post_data_json)
                                    return await r.fulfill(status=200, json=sms)
                                if url == '/api/miniapp/push/read' and fail_read:
                                    fail_read = False
                                    return await r.fulfill(status=503, json={'error': 'Synthetic read failure'})
                                if url == '/api/miniapp/push/read-all':
                                    read_all_payloads.append(r.request.post_data_json)
                                    if not injected:
                                        injected = True
                                        async with sessions() as session:
                                            session.add(Notification(user_id=1, kind='push_test', event_key='arrived-after-open', text='Новое событие после открытия ленты'))
                                            await session.commit()
                                response = await client.request(method, url, data=r.request.post_data_buffer,
                                    headers={'X-Telegram-Init-Data': bound_init, 'Content-Type': r.request.headers.get('content-type', 'application/json')})
                                body = await response.read()
                                if url.startswith('/api/miniapp/push?') and 'before=' in url:
                                    page = json.loads(body)
                                    if page['items']:
                                        page['items'].append(page['items'][0])  # overlapping transport row
                                    body = json.dumps(page).encode()
                                await r.fulfill(status=response.status, headers={'Content-Type': response.headers.get('Content-Type', 'text/plain')}, body=body)
                            return route
                        await context.route('**/*', make_route(init))
                        page = await context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(BASE+'/app/#home')
                        await page.get_by_role('heading', name='Всё под контролем', exact=True).wait_for()
                        assert await page.locator('#bookingControl').count() == 0
                        assert await page.get_by_text('Записи и помощь менеджерам', exact=True).count() == 0
                        assert await page.get_by_text('Подсказки и задания для менеджеров', exact=True).count() == 0
                        await page.locator('#app [data-action=new-booking]').click()
                        form = page.locator('#workflowBooking')
                        await expect(form.locator('[name=client_name]')).to_be_visible()
                        await form.locator('[name=client_name]').fill('Synthetic '+role)
                        await form.locator('[name=room]').fill('QA-'+str(uid))
                        await form.locator('[name=shoot_date]').fill((api.today()+timedelta(days=1)).isoformat())
                        await form.locator('[name=shoot_time]').fill('12:30')
                        await form.locator('[name=deposit]').fill('0')
                        await form.get_by_role('button', name='Сохранить бронь', exact=True).click()
                        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером')
                        async with sessions() as session:
                            assert await session.scalar(select(func.count(Booking.id)).where(Booking.manager_id == uid, Booking.room == 'QA-'+str(uid))) == 1
                        await page.locator('[data-action=app-notifications]').click()
                        center = page.locator('#appNotifications')
                        if uid == 1:
                            await expect(page.locator('[data-notice-count]')).to_have_text('40')
                            await expect(center.locator('[data-notice-id]')).to_have_count(12)
                            assert await center.locator('.notification-summary').filter(has_text='Два разных события с одинаковым текстом').count() == 2
                            await expect(center.locator('.is-important')).to_have_count(1)
                            latest = int(await center.locator('[data-notice-id]').first.get_attribute('data-notice-id'))
                            mark = center.locator(f'[data-notice-mark="{latest}"]')
                            await mark.focus()
                            await page.keyboard.press('Enter')
                            await expect(page.locator('[data-notice-count]')).to_have_text('39')
                            await expect(center.locator(f'[data-notice-id="{latest}"]')).to_have_class('notification-card is-read ')
                            assert await page.evaluate('document.activeElement.dataset.noticeControl') == f'open-{latest}'
                            await page.reload()
                            await page.locator('[data-action=app-notifications]').click()
                            await expect(page.locator('[data-notice-count]')).to_have_text('39')
                            await expect(center.locator(f'[data-notice-id="{latest}"] .notification-read-state')).to_have_text('Прочитано')
                            await center.get_by_role('button', name='Показать более ранние', exact=True).click()
                            await expect(center.locator('[data-notice-id]')).to_have_count(24)
                            await expect(page.locator('[data-notice-count]')).to_have_text('39')
                            await center.get_by_role('button', name='Прочитать всё', exact=True).click()
                            await expect(page.locator('[data-notice-count]')).to_have_text('1')
                            assert read_all_payloads[0] == {'throughId': latest}
                            await expect(center.locator('.notification-summary').filter(has_text='Новое событие после открытия ленты')).to_be_visible()
                            await center.get_by_role('button', name='Прочитать всё', exact=True).click()
                            await expect(page.locator('[data-notice-count]')).to_be_hidden()
                            async with sessions() as session:
                                notice = Notification(user_id=1, kind='push_test', event_key='read-retry', text='Проверка ошибки прочтения')
                                session.add(notice)
                                await session.flush()
                                retry_id = notice.id
                                await session.commit()
                            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                            await expect(page.locator('[data-notice-count]')).to_have_text('1')
                            fail_read = True
                            await center.locator(f'[data-notice-mark="{retry_id}"]').click()
                            await expect(center.locator('[data-notice-feedback]')).to_contain_text('Не удалось сохранить прочтение')
                            await expect(page.locator('[data-notice-count]')).to_have_text('1')
                            await center.locator(f'[data-notice-mark="{retry_id}"]').click()
                            await expect(page.locator('[data-notice-count]')).to_be_hidden()
                            await page.screenshot(path=str(output/f'owner-inbox-{width}.png'), full_page=False)
                            await page.locator('#closeSheet').click()
                            # Real booking card, mocked SMS states: no actual provider.
                            for status, reason, available, expected in [
                                ('unavailable', 'provider_not_configured', False, 'SMS-сервис не подключён'),
                                ('scheduled', None, True, 'SMS запланировано'),
                                ('provider_accepted', None, True, 'доставка не подтверждена'),
                                ('cancelled', 'cancelled', True, 'SMS-напоминание отменено'),
                                ('rescheduled', 'booking_changed', True, 'после переноса')]:
                                sms.update(status=status, reason=reason, available=available, enabled=available, consent=available,
                                    scheduledFor=(utc_now()+timedelta(hours=1)).isoformat()+'Z')
                                await page.evaluate("location.hash='#shootings?booking=1'")
                                box = page.locator('[data-booking-sms="1"]')
                                await expect(box).to_contain_text(expected)
                                await box.locator('summary').click()
                                if not available:
                                    await expect(box.locator('[name=enabled]')).to_be_disabled()
                                else:
                                    await box.locator('[name=leadMinutes]').fill('90')
                                    await box.get_by_role('button', name='Сохранить напоминание').click()
                                    await expect(box.locator('[data-sms-feedback]')).to_contain_text('Настройки сохранены')
                                await page.screenshot(path=str(output/f'sms-{status}-{width}.png'), full_page=False)
                                await page.locator('#closeSheet').click()
                                await page.evaluate("location.hash='#more'")
                                await page.get_by_role('heading', name='Ваше пространство', exact=True).wait_for()
                        else:
                            await expect(center.get_by_text('Пока нет уведомлений', exact=True)).to_be_visible()
                            await expect(center.get_by_role('button', name='Прочитать всё', exact=True)).to_be_disabled()
                            await expect(page.locator('[data-notice-count]')).to_be_hidden()
                            await page.screenshot(path=str(output/f'{role.lower()}-empty-{width}.png'), full_page=False)
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        await context.close()
                    assert not errors, errors
                    assert sms_writes and bot.send_message.await_count == 0
                    print('PASS actual mobile inbox: exact whole-history count, persisted read, keyboard, snapshot read-all, later event retained, dedup pages/distinct events, explicit read failure/retry; HOME new bookings for OWNER/ADMIN/MANAGER; SMS unavailable/scheduled/accepted/cancelled/rescheduled without sends')
                finally:
                    await browser.close()
        finally:
            await client.close()
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
