"""Real HTTPS + service worker cold start + blob replay, using isolated services."""
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
from playwright.async_api import async_playwright
from sqlalchemy import func, select
from test_miniapp_release import TOKEN, signed
from test_workflow_services import booking_data, fixture
from work_calls_browser_check import wait_async

from app.academy_practice import install_academy_practice
from app.attendance import install_attendance
from app.miniapp_api import install_miniapp
from app.models import (
    Booking,
    OperationRequest,
    PayrollEntry,
    Receipt,
    Sale,
    Shooting,
    User,
)
from app.people import install_people
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat
from app.workflow import Workflow


async def main():
    temporary = tempfile.TemporaryDirectory(prefix='photo-boss-offline-')
    engine, factory = await fixture('sqlite+aiosqlite:///' + str(Path(temporary.name) / 'workflow.db'))
    async with factory() as session:
        booking = await create_booking_record(session, await session.get(User, 1), booking_data())
        booking.photographer_id = 2
        booking.status = 'READY_FOR_SALE'
        (await session.scalar(select(Shooting))).status = 'READY_FOR_SALE'
        await session.commit()
    async def upload(_id, file, **kwargs):
        digest = hashlib.sha256(file.data).hexdigest()
        attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
        return SimpleNamespace(photo=[attachment], document=attachment)
    async def save_selected(self, draft, raw):
        return 'app:/fixture/' + hashlib.sha256(raw).hexdigest()
    bot = SimpleNamespace(token=TOKEN, send_photo=upload, send_document=upload,
        send_message=AsyncMock(), me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app = web.Application(client_max_size=25*1024*1024)
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    original = api.rows
    async def rows(conn, sql, **params):
        return await original(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    install_attendance(app, api)
    install_people(app, api)
    chat = install_work_chat(app, api)
    install_work_calls(app, chat)
    install_academy_practice(app, api)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ['WORKFLOW_TEST_CERT'], os.environ['WORKFLOW_TEST_KEY'])
    server = TestServer(app)
    await server.start_server(ssl=tls)
    url = str(server.make_url('/app/#workflow'))
    errors = []
    try:
        with patch.object(Workflow, 'save_selected', save_selected):
            async with async_playwright() as p:
                browser = await p.chromium.launch(args=['--ignore-certificate-errors'])
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844}, is_mobile=True)
                    init = signed(1)
                    await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(init)+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                    await context.route('https://telegram.org/**', lambda r: r.fulfill(status=200, content_type='text/javascript', body=''))
                    lost = False
                    async def drop_once(route):
                        nonlocal lost
                        if route.request.post_data_json.get('kind') == 'sale_complete' and not lost:
                            response = await route.fetch()
                            assert response.status == 200
                            lost = True
                            await route.abort('connectionreset')
                        else:
                            await route.continue_()
                    await context.route('**/api/miniapp/operations/sync', drop_once)
                    page = await context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(url)
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    await page.evaluate('navigator.serviceWorker.ready')
                    await page.wait_for_function('navigator.serviceWorker.controller !== null')
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await context.set_offline(True)
                    assert await page.evaluate("""async()=>{
                        const m=await import('/app/js/outbox.js');
                        try{await m.enqueueBatch([{kind:'booking',date:'2026-09-22',data:{}},
                            {kind:'booking',date:'2026-09-22',data:{invalid(){}}}]);return false;}catch{}
                        return (await m.outboxRows()).length===0;
                    }"""), 'Failed local batch left partial operations'
                    await page.get_by_text('Новая бронь', exact=True).click()
                    form = page.locator('#workflowBooking')
                    await form.locator('[name=client_name]').fill('Offline Guest')
                    await form.locator('[name=room]').fill('200')
                    await form.locator('[name=shoot_time]').fill('14:00')
                    await form.locator('button').click()
                    await page.get_by_text('Сохранено локально', exact=True).wait_for()
                    await page.locator('#workflowSalePick').select_option('1')
                    sale = page.locator('#workflowSale')
                    await sale.locator('[name=total]').fill('150')
                    await sale.locator('[name=sold]').fill('1')
                    await sale.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                    await sale.locator('[name=selected]').set_input_files('app/webapp/assets/academy/family.jpg')
                    await sale.locator('button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).length===6;}")
                    # A new page removes in-memory state; only SW and IndexedDB remain.
                    await page.close()
                    page = await context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(url)
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    await page.get_by_text('Сохранено локально', exact=True).first.wait_for()
                    await context.set_offline(False)
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_complete'&&r.status==='local'&&r.error);}")
                    await page.get_by_role('button', name='Синхронизировать сейчас').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');const rows=await m.outboxRows();return rows.length===6&&rows.every(r=>r.status==='synced'&&!r.blob);}")
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Booking.id))) == 2
                        assert await session.scalar(select(func.count(Sale.id))) == 1
                        assert await session.scalar(select(func.count(Receipt.id))) == 1
                        assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                        assert await session.scalar(select(func.count(OperationRequest.id))) == 6
                        assert (await session.scalar(select(Sale))).amount == 400
                    assert lost and not errors, errors
                    assert await page.evaluate("async()=>{for(const name of await caches.keys()){for(const request of await (await caches.open(name)).keys())if(new URL(request.url).pathname.startsWith('/api/'))return false;}return true;}")
                    output = Path(os.getenv('CALLS_QA_DIR', '/tmp/photo-boss-calls-qa'))
                    output.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(output/'offline-replay-mobile.png'), full_page=True)
                    print('PASS: cold offline start, booking + sale blobs, lost response replay, one sale/commission, no API cache')
                finally:
                    await browser.close()
    finally:
        await server.close()
        await engine.dispose()
        temporary.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
