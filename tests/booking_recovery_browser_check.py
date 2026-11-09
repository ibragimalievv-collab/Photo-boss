"""A rejected booking stays editable and resumes under the same operation key.

Isolated SQLite fixture only; no production data, messages or payments.
"""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_services import fixture

from app.miniapp_api import install_miniapp
from app.models import Booking, Client, OperationRequest, Package, Shooting
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS


async def set_package_active(factory, active):
    async with factory() as session:
        (await session.get(Package, 1)).active = active
        await session.commit()


async def main():
    with tempfile.TemporaryDirectory(prefix='photo-boss-booking-recovery-') as directory:
        engine, factory = await fixture('sqlite+aiosqlite:///' + str(Path(directory) / 'fixture.db'))
        bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock())
        app = web.Application()
        api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
        original_rows = api.rows

        async def rows(conn, sql, **params):
            return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

        api.rows = rows
        cert, key = Path(directory) / 'test-cert.pem', Path(directory) / 'test-key.pem'
        await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                                '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server = TestServer(app)
        await server.start_server(ssl=tls)
        errors = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844})
                    await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(1)) + '}}')
                    await context.route('https://telegram.org/**', lambda route: route.fulfill(body='', content_type='text/javascript'))
                    for prefix in ('shift', 'people', 'work-chat'):
                        await context.route(f'**/{prefix}/**', lambda route: route.fulfill(body='', content_type='text/javascript'))
                    page = await context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(str(server.make_url('/app/#workflow')))
                    await page.get_by_text('Новая бронь', exact=True).click()
                    form = page.locator('#workflowBooking')
                    await form.locator('[name=client_name]').fill('Гость для проверки')
                    await form.locator('[name=client_phone]').fill('+7 999 000-00-00')
                    await form.locator('[name=room]').fill('101')
                    await form.locator('[name=shoot_time]').fill('12:00')
                    await form.locator('[name=deposit]').fill('0')
                    # The phone is now checked on the device with the server's rules, so
                    # the rejection comes from server-only data: the package was switched
                    # off after this screen loaded.
                    await set_package_active(factory, False)
                    await form.get_by_role('button', name='Сохранить бронь', exact=True).click()
                    # The form is free for the next guest at once; the rejected booking
                    # waits in this device's booking list with the server's reason.
                    history = page.locator('#bookingHistory [data-booking-history]')
                    await expect(history).to_have_count(1, timeout=30000)
                    await expect(history).to_contain_text('Запись требует исправления', timeout=30000)
                    await expect(history).to_contain_text('Отель или пакет больше не доступны')
                    await expect(form.locator('[name=client_name]')).to_have_value('')
                    before = await page.evaluate("async()=>await (await import('/app/js/outbox.js')).outboxRows()")
                    assert len(before) == 1 and before[0]['status'] == 'error'
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Booking.id))) == 0
                    await set_package_active(factory, True)  # the owner restores the package
                    await history.get_by_role('button', name='Исправить запись', exact=True).click()
                    correction = page.locator('#workflowBookingCorrection')
                    await expect(correction).to_be_visible()
                    await expect(correction.locator('[role=alert]')).to_contain_text('Отель или пакет больше не доступны')
                    await expect(correction.locator('[name=client_name]')).to_have_value('Гость для проверки')
                    await correction.locator('[name=client_phone]').fill('+79991234567')
                    await correction.get_by_role('button', name='Сохранить исправление', exact=True).click()
                    await expect(history).to_contain_text('сохранена сервером', timeout=30000)
                    await expect(correction).to_have_count(0)
                    after = await page.evaluate("async()=>await (await import('/app/js/outbox.js')).outboxRows()")
                    assert len(after) == 1 and after[0]['key'] == before[0]['key'] and after[0]['status'] == 'synced'
                    async with factory() as session:
                        for model in (Client, Booking, Shooting, OperationRequest):
                            assert await session.scalar(select(func.count(model.id))) == 1
                        assert (await session.scalar(select(Client))).phone == '+79991234567'
                    await page.reload()
                    await page.get_by_text('Новая бронь', exact=True).click()
                    await expect(page.locator('#bookingHistory [data-booking-history]')).to_contain_text('сохранена сервером')
                    assert not errors, errors
                    bot.send_message.assert_not_awaited()
                    print('PASS: a booking the server rejects waits in the device list, is corrected and saved under the original key; one booking/client/shooting, no duplicates after reload')
                finally:
                    await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
