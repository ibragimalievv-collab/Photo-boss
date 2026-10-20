"""An earlier total cannot permanently strand sale photos in the originating browser."""
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
from test_workflow_services import booking_data, fixture

from app.miniapp_api import install_miniapp
from app.models import (
    OperationRequest,
    Photo,
    Receipt,
    Sale,
    SaleDraft,
    Shooting,
    User,
    utc_now,
)
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.bookings import create_booking_record
from app.workflow import Workflow


async def main():
    with tempfile.TemporaryDirectory(prefix='photo-boss-counts-') as directory:
        engine, factory = await fixture('sqlite+aiosqlite:///' + str(Path(directory) / 'counts.db'))
        async with factory() as session:
            booking = await create_booking_record(session, await session.get(User, 1), booking_data())
            booking.photographer_id, booking.status = 2, 'READY_FOR_SALE'
            shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == booking.id))
            shooting.status = 'READY_FOR_SALE'
            session.add_all(Photo(shooting_id=shooting.id, file_id=f'original-{i}') for i in range(10))
            await session.commit()
            bid, sid = booking.id, shooting.id

        async def upload(_uid, file, **_kwargs):
            digest = hashlib.sha256(file.data).hexdigest()
            attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
            return SimpleNamespace(photo=[attachment], document=attachment)
        async def selected(_self, draft, raw):
            return f'app:/test-counts/{draft.id}/{hashlib.sha256(raw).hexdigest()}.jpg'
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
            with patch.object(Workflow, 'save_selected', selected), patch('app.workflow.extract_receipt', AsyncMock(return_value={'status':'unavailable'})):
                async with async_playwright() as p:
                    browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
                    try:
                        context = await browser.new_context(ignore_https_errors=True, viewport={'width':390,'height':844})
                        await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(signed(1))+'}}')
                        await context.route('https://telegram.org/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                        for prefix in ('shift', 'people', 'work-chat'):
                            await context.route(f'**/{prefix}/**', lambda r: r.fulfill(body='', content_type='text/javascript'))
                        page = await context.new_page()
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        await page.goto(str(server.make_url('/app/#workflow')))
                        await page.locator('#workflowSalePick').select_option(str(bid))
                        form = page.locator('#workflowSale')
                        await form.locator('[name=total]').fill('150')
                        await form.locator('[name=sold]').fill('1')
                        await form.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                        await form.locator('[name=selected]').set_input_files('app/webapp/assets/academy/family.jpg')
                        # Another photographer finishes the original upload after
                        # this form was opened. The server now requires ten total.
                        async with factory() as session:
                            (await session.get(Shooting, sid)).full_upload_completed_at = utc_now()
                            await session.commit()
                        await form.locator('button').first.click()
                        repair = form.locator('[data-workflow="repair-sale"]')
                        await expect(repair).to_be_visible(timeout=20000)
                        before = await page.evaluate("""async()=>{
                            const {outboxRows}=await import('/app/js/outbox.js');return await outboxRows();
                        }""")
                        counts = next(row for row in before if row['kind'] == 'sale_counts')
                        assert counts['status'] == 'error' and counts['errorStatus'] == 409
                        assert next(row for row in before if row['kind'] == 'sale_selected')['hasBlob']
                        async with factory() as session:
                            assert (await session.scalar(select(SaleDraft))).status == 'AWAITING_COUNTS'
                            assert await session.scalar(select(func.count(Sale.id))) == 0
                            assert await session.scalar(select(OperationRequest.id).where(OperationRequest.request_key == counts['key'])) is None
                        await page.reload()
                        await expect(repair).to_be_visible()
                        await repair.click()
                        await expect(form.locator('[name=total]')).to_have_value('10')
                        await expect(form.locator('[name=sold]')).to_have_value('1')
                        await expect(form.locator('[name=sold]')).to_be_disabled()
                        await form.get_by_role('button', name='Сохранить общее количество и продолжить').click()
                        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=20000)
                        await expect(form.locator('[data-sale-photo-progress]')).to_contain_text('Файлов принято 1 из 1')
                        after = await page.evaluate("async()=>await (await import('/app/js/outbox.js')).outboxRows()")
                        assert [row['key'] for row in after] == [row['key'] for row in before]
                        assert all(row['status'] == 'synced' for row in after)
                        assert next(row for row in after if row['kind'] == 'sale_counts')['data']['total'] == 10
                        for original in before:
                            row = next(row for row in after if row['key'] == original['key'])
                            if row['kind'] != 'sale_counts':
                                assert row['data'] == original['data']
                        async with factory() as session:
                            saved = list((await session.scalars(select(Sale))).all())
                            assert len(saved) == 1 and saved[0].amount == 400 and saved[0].sold_photos == 1
                            assert await session.scalar(select(func.count(Receipt.id))) == 1
                        # A second test purchase reuses that earlier receipt.
                        # The server must reject it, and the same form must let
                        # its author attach the actual new receipt in place.
                        await form.get_by_role('button', name='Начать новую операцию').click()
                        await form.locator('[name=sold]').fill('1')
                        await form.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                        await form.locator('[name=selected]').set_input_files('app/webapp/assets/academy/evening.jpg')
                        await form.get_by_role('button', name='Добавить допродажу').click()
                        replacement = page.locator('#workflowQueue [data-workflow-replace]')
                        await expect(replacement).to_be_visible(timeout=20000)
                        receipt_key = await replacement.get_attribute('data-workflow-replace')
                        original_keys = await page.evaluate("async()=> (await (await import('/app/js/outbox.js')).outboxRows()).map(r=>r.key)")
                        async with factory() as session:
                            assert await session.scalar(select(func.count(Sale.id))) == 1
                            assert await session.scalar(select(func.count(Receipt.id))) == 1
                            assert await session.scalar(select(OperationRequest.id).where(OperationRequest.request_key == receipt_key)) is None
                        await replacement.set_input_files('app/webapp/assets/academy/child.jpg')
                        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=20000)
                        assert await page.evaluate("async()=> (await (await import('/app/js/outbox.js')).outboxRows()).map(r=>r.key)") == original_keys
                        async with factory() as session:
                            saved = list((await session.scalars(select(Sale))).all())
                            assert len(saved) == 2 and all(row.amount == 400 and row.sold_photos == 1 for row in saved)
                            assert await session.scalar(select(func.count(Receipt.id))) == 2
                        assert not errors, errors
                        print('PASS: counts409 recovers after reload; duplicate receipt409 replaces in place; original keys/photos retained; two separate 400-ruble sales and two receipts')
                    finally:
                        await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
