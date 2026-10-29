"""Real browser + application API: append a return purchase, never overwrite a paid sale."""
import asyncio
import hashlib
import json
import os
import ssl
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_sales_followups import prepared, purchase

from app.miniapp_api import install_miniapp
from app.models import Booking, HotelTariff, OperationRequest, Receipt, Sale
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.receipts import payment_totals, refresh_payment_statuses
from app.workflow import Workflow


async def run(url):
    engine, factory, bid = await prepared(url)
    async with factory() as session:
        original = await purchase(session, bid, 1, 2,
            repeated_hash=hashlib.sha256(Path('app/webapp/assets/academy/child.jpg').read_bytes()).hexdigest())
        session.add_all(HotelTariff(hotel_id=1, name=f'{price} ₽ за кадр', kind='UNIT', price=price)
                        for price in (300, 400, 500))
        original.receipt.status, original.receipt.verified_amount = 'APPROVED', 800
        await refresh_payment_statuses(session, bid)
        (await session.get(Booking, bid)).status = 'COMPLETED'
        await session.commit()
        original_id = original.sale.id
    async def upload(_uid, file, **_kwargs):
        digest = hashlib.sha256(file.data).hexdigest()
        attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
        return SimpleNamespace(photo=[attachment], document=attachment)
    async def selected(_self, draft, raw):
        return f'app:/test-sales/{draft.id}/{hashlib.sha256(raw).hexdigest()}.jpg'
    bot = SimpleNamespace(token=TOKEN, send_photo=upload, send_document=upload, send_message=AsyncMock())
    app = web.Application(client_max_size=25*1024*1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    raw_rows = api.rows
    async def rows(conn, sql, **params):
        return await raw_rows(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ['WORKFLOW_TEST_CERT'], os.environ['WORKFLOW_TEST_KEY'])
    server = TestServer(app)
    await server.start_server(ssl=tls)
    errors = []
    try:
        with patch.object(Workflow, 'save_selected', selected):
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width':390,'height':844})
                    await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(signed(1))+'}}')
                    await context.route('https://telegram.org/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                    # Focus this test on sales; other modules have their own browser suites.
                    for prefix in ('shift', 'people', 'work-chat'):
                        await context.route(f'**/{prefix}/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                    page = await context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(str(server.make_url('/app/#workflow')))
                    await page.locator(f'[data-workflow="sale-history"][data-booking="{bid}"]').click()
                    form = page.locator('#workflowSale')
                    await expect(form.locator('[name=total]')).to_have_value('10')
                    assert await form.locator('[name=total]').get_attribute('type') == 'hidden'
                    await expect(page.get_by_role('heading', name='История покупок')).to_be_visible()
                    tariff = await form.locator('[name=tariffId] option').evaluate_all(
                        "options=>options.find(option=>option.textContent.includes('400')).value")
                    await form.locator('.sale-details').evaluate('el=>el.open=true')
                    await form.locator('[name=tariffId]').select_option(tariff)
                    await form.locator('[name=sold]').fill('3')
                    await form.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                    await form.locator('[name=selected]').set_input_files([
                        'app/webapp/assets/academy/child.jpg',
                        'app/webapp/assets/academy/couple.jpg',
                        'app/webapp/assets/academy/evening.jpg',
                    ])
                    await form.get_by_role('button', name='Сохранить продажу').click()
                    replacement = page.locator('#workflowQueue [data-workflow-replace]')
                    await expect(replacement).to_be_visible()
                    duplicate_key = await replacement.get_attribute('data-workflow-replace')
                    queued_keys = await page.evaluate("async()=>{const {outboxRows}=await import('/app/js/outbox.js');return (await outboxRows()).map(r=>r.key);}")
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Sale.id))) == 1
                        assert await session.scalar(select(func.count(Receipt.id))) == 1
                        assert await session.scalar(select(OperationRequest.id).where(OperationRequest.request_key == duplicate_key)) is None
                    await replacement.set_input_files('app/webapp/assets/academy/family.jpg')
                    await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=20000)
                    assert await page.evaluate("""async([key,keys])=>{
                        const {outboxRows}=await import('/app/js/outbox.js'),rows=await outboxRows();
                        return JSON.stringify(rows.map(r=>r.key))===JSON.stringify(keys)&&
                            rows.find(r=>r.key===key)?.status==='synced'&&rows.find(r=>r.key===key)?.filename==='family.jpg';
                    }""", [duplicate_key, queued_keys]), 'Corrected selection must keep the original sale operations'
                    async with factory() as session:
                        sales = list((await session.scalars(select(Sale).order_by(Sale.id))).all())
                        assert len(sales) == 2
                        assert (sales[0].id, sales[0].amount, sales[0].sold_photos, sales[0].payment_status) == (original_id, 800, 2, 'PAID'), [
                            (sale.id, sale.amount, sale.sold_photos, sale.payment_status) for sale in sales
                        ]
                        assert (sales[1].amount, sales[1].sold_photos, sales[1].payment_status) == (1200, 3, 'UNPAID')
                        assert tuple(await payment_totals(session, bid)) == (2000,800,1200)
                        assert await session.scalar(select(func.count(Receipt.id))) == 2
                        operation_count = await session.scalar(select(func.count(OperationRequest.id)))
                    # Repeated form submission cannot duplicate the stored sale or its payment.
                    await page.evaluate("async()=>{const m=await import('/app/js/workflow.js');try{await m.workflowSubmit(document.querySelector('#workflowSale'));}catch(e){if(!e.message.includes('уже сохранена'))throw e;}}")
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Sale.id))) == 2
                        assert await session.scalar(select(func.count(OperationRequest.id))) == operation_count
                    await form.get_by_role('button', name='Начать новую операцию').click()
                    await expect(page.get_by_role('heading', name='История покупок')).to_be_visible()
                    await expect(page.locator('#workflowSale')).to_contain_text('Количество покупки — исправить')
                    assert not errors, errors
                    output = Path('/tmp/photo-boss-calls-qa'); output.mkdir(exist_ok=True, parents=True)
                    await page.screenshot(path=str(output/'sales-followup-mobile.png'), full_page=True)
                    print('PASS: return purchase, duplicate-photo409 corrected in-place with same key, single new receipt; 800+1200 as separate sales; first payment intact; duplicate submit rejected; history restored')
                finally:
                    await browser.close()
    finally:
        try:
            await server.close()
        finally:
            await engine.dispose()


async def main():
    # Background-task cancellation can replace a SQLite connection at shutdown.
    # Keep the disposable browser database independent of any one connection.
    with tempfile.TemporaryDirectory(prefix='photo-boss-followup-') as directory:
        await run('sqlite+aiosqlite:///' + str(Path(directory) / 'followup.db'))


if __name__ == '__main__':
    asyncio.run(main())
