"""Real frontend + IndexedDB + disposable ledger; all bank responses synthetic.

Run separately with Playwright/Chromium installed. No Sber, Telegram or cloud
storage request is permitted; production mode below is only a local fixture.
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
from playwright.async_api import async_playwright
from sqlalchemy import func, select, text
from test_miniapp_release import TOKEN, signed
from test_workflow_services import booking_data, fixture
from work_calls_browser_check import wait_async

from app.attendance import install_attendance
from app.miniapp_api import install_miniapp
from app.models import (
    BankPayment,
    Booking,
    PayrollEntry,
    Receipt,
    Sale,
    SaleDraftPhoto,
    Shooting,
    User,
)
from app.people import install_people
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]


async def queue(page):
    return await page.evaluate("async()=>{const m=await import('/app/js/outbox.js');return m.outboxRows();}")


async def sync_now(page):
    await page.evaluate("async()=>{const m=await import('/app/js/outbox.js');await m.syncOutboxNow();}")


async def wait_gate(page, kind, status):
    await wait_async(page, """async({kind,status})=>{
      const m=await import('/app/js/outbox.js');
      return (await m.outboxRows()).some(row=>row.kind===kind&&row.result?.status===status);
    }""", arg={'kind': kind, 'status': status})
    return next(row for row in await queue(page) if row['kind'] == kind and row.get('result', {}).get('status') == status)


async def main():
    with tempfile.TemporaryDirectory(prefix='photo-boss-qr-browser-') as folder:
        certificate = Path(folder) / 'synthetic-cert.b64'
        certificate.write_text('Synthetic readiness fixture. TLS is covered separately.')
        environment = {key: '' for key in os.environ if key.startswith('SBER_')}
        environment.update({
            'SBER_QR_ENABLED': '1', 'SBER_QR_ENV': 'production',
            'SBER_QR_CLIENT_ID': 'synthetic-local-client', 'SBER_QR_CLIENT_SECRET': 'synthetic-local-secret',
            'SBER_QR_CERT_PASSWORD': 'synthetic-local-password', 'SBER_QR_CERT_FILE': str(certificate),
            'SBER_QR_CERT_FORMAT': 'p12-base64', 'SBER_QR_ENDPOINTS_CONFIRMED': '1',
            'SBER_QR_MEMBER_ID': 'MTEST001', 'SBER_QR_ID_QR': 'synthetic-local-device', 'SBER_QR_TID': 'TTEST001',
            'SBER_QR_API_BASE_URL': 'https://mc.api.sberbank.ru/prod',
            'SBER_QR_TOKEN_URL': 'https://mc.api.sberbank.ru/prod/tokens/v3/oauth',
            'SBER_QR_SUCCESS_CODE': '0', 'SBER_QR_OPERATION_SUCCESS_CODE': '00',
        })
        with patch.dict(os.environ, environment):
            await scenario(folder)


async def scenario(folder):
    engine, sessions = await fixture('sqlite+aiosqlite:///' + str(Path(folder) / 'ledger.db') + '?timeout=15')
    async with engine.connect() as connection:
        assert (await connection.scalar(text('PRAGMA journal_mode=WAL'))).lower() == 'wal'
        await connection.commit()
    orders, status_calls, saved_media, errors = {}, [], [], []

    def qr_client(config, **kwargs):
        test_mode = config.environment == 'sandbox'

        async def create(number, amount, description):
            assert number not in orders, 'One immutable bank order per gate'
            orders[number] = {'id': f'synthetic-order-{len(orders)+1}', 'amount': amount,
                              'test': test_mode, 'status': 'PENDING'}
            return {'bank_order_id': orders[number]['id'], 'status': 'PENDING', 'test_mode': test_mode,
                    'payment_url': 'https://qr.nspk.ru/' + orders[number]['id']}

        async def status(order_id, number, amount):
            order = orders[number]
            assert order_id == order['id'] and amount == order['amount'] and test_mode == order['test']
            status_calls.append(number)
            paid = order['status'] in {'PAID', 'TEST_PAID'}
            return {'bank_order_id': order_id, 'order_number': number, 'currency': '643',
                    'amount_kopecks': amount, 'paid_kopecks': amount if paid else 0,
                    'refunded_kopecks': 0, 'test_mode': test_mode, 'status': order['status'],
                    'settlement_verified': paid}

        return SimpleNamespace(create_order=create, get_status=status)

    async def save_selected(self, draft, raw):
        saved_media.append(hashlib.sha256(raw).hexdigest())
        return 'app:/synthetic-qr-browser/' + saved_media[-1]

    bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(), send_photo=AsyncMock(), send_document=AsyncMock(),
                          me=AsyncMock(return_value=SimpleNamespace(username='synthetic_fixture_bot')))
    app = web.Application(client_max_size=25 * 1024 * 1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    original_rows = api.rows

    async def sqlite_rows(connection, sql, **params):
        return await original_rows(connection, sql.replace(' FOR UPDATE', ''), **params)

    api.rows = sqlite_rows
    install_attendance(app, api)
    install_people(app, api)
    certificate, key = Path(folder) / 'test-cert.pem', Path(folder) / 'test-key.pem'
    await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(certificate), '-days', '1', '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server = TestServer(app)
    await server.start_server(ssl=tls)
    try:
        with patch('app.bank_payments.QRClient.from_config', qr_client), patch.object(Workflow, 'save_selected', save_selected):
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844}, is_mobile=True)
                    await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(1)) +
                        ',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                    await context.route('https://telegram.org/**', lambda route: route.fulfill(status=200, content_type='text/javascript', body=''))
                    page = await context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(str(server.make_url('/app/#workflow')))
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    await page.get_by_text('Новая бронь', exact=True).click()
                    booking = page.locator('#workflowBooking')
                    await booking.locator('[name=client_name]').fill('Synthetic QR Guest')
                    await booking.locator('[name=room]').fill('200')
                    await booking.locator('[name=shoot_time]').fill('14:00')
                    await booking.locator('[name=deposit]').fill('200')
                    await booking.locator('[name=paymentMethod]').select_option('bank')
                    assert not orders, 'Typing or selecting payment must not register a bill'
                    await booking.locator('button:not([type]),button[type=submit]').click()
                    deposit_gate = await wait_gate(page, 'booking_bank_payment', 'PENDING')
                    bid = deposit_gate['result']['bookingId']
                    await booking.locator('[data-auto-bank-qr] img').wait_for()
                    assert len(orders) == 1
                    async with sessions() as session:
                        assert await session.scalar(select(func.count(Receipt.id))) == 0
                        assert await session.scalar(select(func.count(Booking.id))) == 1
                    next(iter(orders.values()))['status'] = 'PAID'
                    await sync_now(page)
                    await wait_gate(page, 'booking_bank_payment', 'PAID')
                    async with sessions() as session:
                        booked = await session.get(Booking, bid)
                        booked.photographer_id, booked.status = 2, 'READY_FOR_SALE'
                        (await session.scalar(select(Shooting).where(Shooting.booking_id == bid))).status = 'READY_FOR_SALE'
                        await session.commit()
                    await page.evaluate("async id=>{const m=await import('/app/js/workflow.js');await m.loadWorkflow(id);m.workflowSelect(id);}", bid)
                    sale = page.locator('#workflowSale')
                    await sale.locator('[name=total]').fill('10')
                    await sale.locator('[name=sold]').fill('1')
                    await sale.locator('[name=paymentMethod]').select_option('bank')
                    await sale.locator('[name=selected]').set_input_files(str(ROOT / 'app/webapp/assets/academy/family.jpg'))
                    await sale.locator('button:not([type]),button[type=submit]').click()
                    purchase_gate = await wait_gate(page, 'sale_bank_payment', 'PENDING')
                    assert purchase_gate['result']['amount'] == '200.00', 'Confirmed deposit reduces the invoice once'
                    await sale.locator('[data-auto-bank-qr] img').wait_for()
                    assert len(orders) == 2 and not saved_media
                    assert await sale.locator('[name=selected]').evaluate('el=>el.files.length') == 1
                    assert any(row['kind'] == 'sale_selected' and row['hasBlob'] for row in await queue(page))
                    async with sessions() as session:
                        assert await session.scalar(select(func.count(Sale.id))) == 0
                        assert await session.scalar(select(func.count(SaleDraftPhoto.id))) == 0
                    # Reload destroys File-input memory; the durable selected
                    # original must still wait behind exactly the same invoice.
                    await page.reload()
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    assert any(row['kind'] == 'sale_selected' and row['hasBlob'] for row in await queue(page))
                    assert len(orders) == 2 and not saved_media
                    async with sessions() as session:
                        bill = await session.get(BankPayment, purchase_gate['result']['id'])
                        orders[bill.order_number]['status'] = 'PAID'
                    await sync_now(page)
                    await wait_async(page, """async()=>{const m=await import('/app/js/outbox.js');
                      return (await m.outboxRows()).some(row=>row.kind==='sale_complete'&&row.status==='synced');}""")
                    await sync_now(page)
                    async with sessions() as session:
                        sale_record = await session.scalar(select(Sale).where(Sale.booking_id == bid))
                        assert sale_record.amount == 400 and sale_record.payment_status == 'PAID'
                        assert await session.scalar(select(func.count(Receipt.id))) == 2
                        assert await session.scalar(select(func.sum(Receipt.verified_amount))) == 400
                        assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                    assert len(orders) == 2 and len(saved_media) == 1
                    assert all(not row['hasBlob'] for row in await queue(page))
                    # Separate browser storage and a separate synthetic booking
                    # prove TEST_PAID cannot release a real sale or its original.
                    await context.close()
                    os.environ.update(SBER_QR_ENV='sandbox', SBER_QR_API_BASE_URL='https://dev.api.sberbank.ru/sandbox',
                                      SBER_QR_TOKEN_URL='https://dev.api.sberbank.ru/sandbox/tokens/v3/oauth')
                    async with sessions() as session:
                        test_booking = await create_booking_record(session, await session.get(User, 1),
                            booking_data() | {'shoot_date': api.today().isoformat(), 'client_name': 'Synthetic Sandbox Guest'})
                        test_booking.photographer_id, test_booking.status = 2, 'READY_FOR_SALE'
                        await session.commit()
                        test_bid = test_booking.id
                    context = await browser.new_context(ignore_https_errors=True)
                    await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(signed(1)) + ',ready(){},expand(){},onEvent(){}}};')
                    await context.route('https://telegram.org/**', lambda route: route.fulfill(status=200, content_type='text/javascript', body=''))
                    page = await context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(str(server.make_url('/app/#workflow?booking=' + str(test_bid))))
                    await page.locator('#workflowSale').wait_for()
                    sale = page.locator('#workflowSale')
                    await sale.locator('[name=total]').fill('10')
                    await sale.locator('[name=sold]').fill('1')
                    await sale.locator('[name=paymentMethod]').select_option('bank')
                    await sale.locator('[name=selected]').set_input_files(str(ROOT / 'app/webapp/assets/academy/coast.jpg'))
                    await sale.locator('button:not([type]),button[type=submit]').click()
                    test_gate = await wait_gate(page, 'sale_bank_payment', 'PENDING')
                    async with sessions() as session:
                        bill = await session.get(BankPayment, test_gate['result']['id'])
                        orders[bill.order_number]['status'] = 'TEST_PAID'
                    await sync_now(page)
                    await wait_gate(page, 'sale_bank_payment', 'TEST_PAID')
                    async with sessions() as session:
                        assert await session.scalar(select(func.count(Sale.id))) == 1
                        assert await session.scalar(select(func.count(Receipt.id))) == 2
                    assert len(orders) == 3 and len(saved_media) == 1
                    assert any(row['kind'] == 'sale_selected' and row['hasBlob'] for row in await queue(page))
                    assert not errors, errors
                    assert status_calls
                    print('PASS: automatic booking QR, deposit deducted once, durable selected original held across reload, one paid sale, sandbox cannot release money or photos')
                finally:
                    await browser.close()
    finally:
        await server.close()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
