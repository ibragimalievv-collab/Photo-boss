"""Real browser: request/approve 50%, three frames, frozen album and one deposit.

Disposable SQLite and synthetic fixtures only; storage/OCR are stubbed. No live
guest photos, notifications or bank transactions are sent.
"""
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_services import booking_data, fixture

from app.miniapp_api import install_miniapp
from app.models import (
    HotelAlbumTariff,
    Notification,
    Receipt,
    Sale,
    SaleDiscountApproval,
    Shooting,
    User,
    UserRole,
)
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.workflow import Workflow


async def main():
    with tempfile.TemporaryDirectory(prefix='photo-boss-pricing-approval-') as directory:
        root = Path(directory)
        engine, factory = await fixture('sqlite+aiosqlite:///' + str(root / 'fixture.db'))
        async with factory() as session:
            session.add(UserRole(user_id=1, role='OWNER'))
            booking = await create_booking_record(session, await session.get(User, 1), booking_data() | {'deposit': '1000'})
            booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
            (await session.scalar(select(Shooting).where(Shooting.booking_id == booking.id))).status = 'READY_FOR_SALE'
            session.add_all([
                HotelAlbumTariff(id=1, hotel_id=booking.hotel_id, name='Маленький альбом', price=10000),
                Receipt(booking_id=booking.id, uploaded_by_id=1, purpose='DEPOSIT',
                        file_id='fixture-deposit', file_unique_id='fixture-deposit', expected_amount=1000,
                        verified_amount=1000, status='APPROVED'),
            ])
            await session.commit()
            bid = booking.id

        async def upload(_uid, file, **_kwargs):
            digest = hashlib.sha256(file.data).hexdigest()
            attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
            return SimpleNamespace(photo=[attachment], document=attachment)

        async def selected(_self, draft, raw):
            return f'app:/fixture-pricing/{draft.id}/{hashlib.sha256(raw).hexdigest()}.jpg'

        bot = SimpleNamespace(token=TOKEN, send_photo=upload, send_document=upload, send_message=AsyncMock())
        app = web.Application(client_max_size=25*1024*1024)
        api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
        original_rows = api.rows

        async def rows(conn, sql, **params):
            return await original_rows(conn, sql.replace(' FOR UPDATE', ''), **params)

        api.rows = rows
        cert, key = root / 'test-cert.pem', root / 'test-key.pem'
        await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                                '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server = TestServer(app)
        await server.start_server(ssl=tls)
        errors = []
        try:
            with patch.object(Workflow, 'save_selected', selected), patch('app.workflow.extract_receipt', AsyncMock(return_value={'status': 'unavailable'})):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                    try:
                        context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844})
                        await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(1)) + '}}')
                        await context.route('https://telegram.org/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                        for prefix in ('shift', 'people', 'work-chat'):
                            await context.route(f'**/{prefix}/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                        page = await context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(str(server.make_url('/app/#workflow')))
                        await page.locator('#workflowSalePick').select_option(str(bid))
                        form = page.locator('#workflowSale')
                        await form.locator('[name=total]').fill('3')
                        await form.locator('[name=sold]').fill('3')
                        await page.get_by_role('button', name='Запросить скидку 50% на всю съёмку', exact=True).click()
                        panel = page.locator('#saleDiscountPanel')
                        await expect(panel).to_contain_text('Ожидает подтверждения')
                        await page.get_by_role('button', name='Запросить скидку 50% на всю съёмку', exact=True).click()
                        await expect(panel).to_contain_text('Ожидает подтверждения')
                        async with factory() as session:
                            assert await session.scalar(select(func.count(SaleDiscountApproval.id))) == 1
                            assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'sale_discount')) == 1
                        await panel.get_by_role('button', name='Подтвердить', exact=True).click()
                        await expect(panel).to_contain_text('Подтверждён')
                        await panel.locator('[name=discountId]').select_option(index=1)
                        await form.locator('[name=total]').fill('3')
                        await form.locator('[name=sold]').fill('3')
                        await form.locator('[name=albumId]').select_option('1')
                        await form.locator('[name=albumQuantity]').fill('2')
                        await expect(form.locator('#salePricePreview')).to_contain_text('20\u00a0600')
                        await expect(form.locator('#salePricePreview')).to_contain_text('19\u00a0600')
                        await form.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                        await form.locator('[name=selected]').set_input_files([
                            'app/webapp/assets/academy/family.jpg', 'app/webapp/assets/academy/child.jpg',
                            'app/webapp/assets/academy/couple.jpg'])
                        await form.get_by_role('button', name='Сохранить продажу и фотографии', exact=True).click()
                        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=30000)
                        async with factory() as session:
                            sales = list(await session.scalars(select(Sale)))
                            assert len(sales) == 1 and sales[0].sold_photos == 3 and sales[0].amount == 20600
                            quote = json.loads(sales[0].pricing_snapshot)
                            assert [line['amount'] for line in quote['lines']] == ['1200.00', '-600.00', '20000.00']
                            receipt = await session.scalar(select(Receipt).where(Receipt.purpose == 'PAYMENT'))
                            assert receipt.expected_amount == 19600
                            assert await session.scalar(select(func.count(Notification.id)).where(Notification.kind == 'sale_discount')) == 2
                        assert not errors, errors
                        print('PASS: mobile browser requested and approved 50% once; 3 frames + 2 albums = 20600; confirmed deposit1000 applied once; receipt balance19600; one completed sale')
                    finally:
                        await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
