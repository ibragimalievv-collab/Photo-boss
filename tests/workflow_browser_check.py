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
    ShiftCheckIn,
    ShiftCheckOut,
    Shooting,
    User,
    UserRole,
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
        session.add_all([User(id=3, tg_id=3, name='Owner'), UserRole(user_id=3, role='OWNER')])
        await session.commit()
    media = {}
    async def upload(_id, file, **kwargs):
        digest = hashlib.sha256(file.data).hexdigest()
        media[digest] = file.data
        attachment = SimpleNamespace(file_id=digest, file_unique_id=digest)
        return SimpleNamespace(photo=[attachment], document=attachment)
    async def save_selected(self, draft, raw):
        return 'app:/fixture/' + hashlib.sha256(raw).hexdigest()
    async def download(file_id, destination, **kwargs):
        destination.write(media[file_id])
    bot = SimpleNamespace(token=TOKEN, send_photo=upload, send_document=upload,
        send_message=AsyncMock(), download=download, me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
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
                browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None,args=['--ignore-certificate-errors', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'])
                try:
                    context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844}, is_mobile=True,
                        geolocation={'latitude': 41.5, 'longitude': 48.1}, permissions=['geolocation', 'camera'])
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
                    receipt_failed = False
                    async def fail_receipt_once(route):
                        nonlocal receipt_failed
                        if not receipt_failed and b'"kind":"sale_receipt"' in (route.request.post_data_buffer or b''):
                            receipt_failed = True
                            await route.fulfill(status=503, content_type='application/json',
                                body=json.dumps({'error': 'Receipt storage temporarily unavailable'}))
                        else:
                            await route.continue_()
                    await context.route('**/api/miniapp/operations/media', fail_receipt_once)
                    page = await context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(url)
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    await page.evaluate('navigator.serviceWorker.ready')
                    await wait_async(page, '()=>navigator.serviceWorker.controller !== null')
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.get_by_text('Новая бронь', exact=True).click()
                    await page.locator('#workflowBooking [name=client_name]').fill('Черновик не терять')
                    await page.evaluate("window.dispatchEvent(new Event('online'))")
                    await page.wait_for_timeout(500)
                    assert await page.locator('#workflowBooking [name=client_name]').input_value() == 'Черновик не терять'
                    await page.locator('#workflowSalePick').select_option('1')
                    await page.locator('#workflowSale [name=total]').fill('150')
                    await page.locator('#workflowSale [name=selected]').set_input_files('app/webapp/assets/academy/family.jpg')
                    await page.locator('#workflowSalePick').select_option('')
                    await page.locator('#workflowSalePick').select_option('1')
                    assert await page.locator('#workflowSale [name=total]').input_value() == '150'
                    assert await page.locator('#workflowSale [name=selected]').evaluate('(el)=>el.files.length') == 1
                    assert await page.locator('#workflowBooking [name=client_name]').input_value() == 'Черновик не терять'
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
                    await page.get_by_text('В очереди на этом устройстве — не отправлено', exact=True).wait_for()
                    await page.locator('#workflowSalePick').select_option('1')
                    sale = page.locator('#workflowSale')
                    await sale.locator('[name=total]').fill('150')
                    await sale.locator('[name=sold]').fill('1')
                    await sale.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/coast.jpg')
                    await sale.locator('[name=selected]').set_input_files('app/webapp/assets/academy/family.jpg')
                    await sale.locator('button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).length===6;}")
                    assert await page.locator('#workflowSale [name=total]').input_value() == '150'
                    await page.evaluate("async()=>{const m=await import('/app/js/workflow.js');await m.workflowSubmit(document.querySelector('#workflowSale'));}")
                    assert await page.evaluate("async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).length;}") == 6

                    await page.locator('#attendanceCard button').click()
                    dialog = page.locator('#attendanceDialog')
                    for _ in range(2):
                        await dialog.locator('[data-att=location]').click()
                        await dialog.locator('[data-att=camera]:not([disabled])').click()
                        await wait_async(page, "()=>document.querySelector('#attCameraVideo')?.videoWidth>0")
                        await dialog.locator('[data-att=snap]').click()
                        await dialog.locator('[data-att=confirm]').click()
                    await page.get_by_role('heading', name='Итоги смены', exact=True).wait_for()
                    await page.locator('#shiftNote [name=note]').fill('Connection was unavailable at shift close')
                    await page.locator('#shiftNote button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).length===9;}")
                    # A new page removes in-memory state; only SW and IndexedDB remain.
                    await page.close()
                    page = await context.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(url)
                    await page.get_by_role('heading', name='Рабочие операции', exact=True).wait_for()
                    await page.get_by_text('В очереди на этом устройстве — не отправлено', exact=True).first.wait_for()
                    await context.set_offline(False)
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_receipt'&&r.status==='local'&&r.errorStatus===503);}")
                    assert await page.evaluate("""async()=>{
                        const m=await import('/app/js/outbox.js'),rows=await m.outboxRows();
                        return rows.filter(r=>['sale_counts','sale_selected','sale_complete'].includes(r.kind))
                            .every(r=>r.status==='local'&&!r.error);
                    }"""), 'Receipt failure must wait without permanently rejecting the dependent sale stages'
                    # Reproduce a previously saved 3.20 queue: the old client
                    # already received wrong-stage 409s after its receipt failed.
                    await page.evaluate("""async()=>{
                        const {outboxRows}=await import('/app/js/outbox.js');
                        const {localAction}=await import('/app/js/localstore.js');
                        const errors={sale_counts:'Черновик продажи уже закрыт.',
                            sale_selected:'Сначала сохраните чек и количество кадров.',
                            sale_complete:'Продажа уже завершена или отменена.'};
                        for(const item of await outboxRows())if(errors[item.kind]){
                            const row=await localAction('operations','readonly',s=>s.get(item.key));
                            row.status='error';row.errorStatus=409;row.error=errors[row.kind];
                            await localAction('operations','readwrite',s=>s.put(row));
                        }
                    }""")
                    await page.get_by_role('button', name='Синхронизировать сейчас').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_complete'&&r.status==='local'&&r.error);}")
                    await page.get_by_role('button', name='Синхронизировать сейчас').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');const rows=await m.outboxRows();return rows.length===9&&rows.every(r=>r.status==='synced'&&!r.blob);}")
                    await page.locator('[data-sale-booking="1"]').filter(has_text='Завершено: 1 фото').wait_for()
                    assert 'завершённых продаж — 1, продано кадров — 1' in await page.locator('[data-sales-totals]').inner_text()
                    assert await page.evaluate("""async()=>{
                        const {outboxRows}=await import('/app/js/outbox.js');
                        return (await outboxRows()).filter(r=>['sale_counts','sale_selected','sale_complete'].includes(r.kind))
                            .every(r=>r.saleDependencyRecovery==='sale-dependency-recovery-v1-20261017');
                    }"""), 'Previously saved wrong-stage failures must recover with the same operations'
                    async with factory() as session:
                        assert await session.scalar(select(func.count(Booking.id))) == 2
                        assert await session.scalar(select(func.count(Sale.id))) == 1
                        assert await session.scalar(select(func.count(Receipt.id))) == 1
                        assert await session.scalar(select(func.count(PayrollEntry.id))) == 1
                        assert await session.scalar(select(func.count(OperationRequest.id))) == 9
                        assert (await session.scalar(select(Sale))).amount == 400
                        assert (await session.scalar(select(ShiftCheckIn))).status == 'PENDING_REVIEW'
                        end = await session.scalar(select(ShiftCheckOut))
                        assert end.status == 'PENDING_REVIEW' and end.report_note == 'Connection was unavailable at shift close'
                    assert lost and receipt_failed and not errors, errors
                    # A completed booking must not trap the next guest behind
                    # its saved, disabled draft or a newly collapsed section.
                    await page.get_by_text('Новая бронь', exact=True).click()
                    await page.locator('#workflowBooking [name=client_name]').fill('First Online Guest')
                    await page.locator('#workflowBooking [name=room]').fill('201')
                    await page.locator('#workflowBooking [name=shoot_time]').fill('15:00')
                    await page.locator('#workflowBooking button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).filter(r=>r.kind==='booking'&&r.status==='synced').length===2;}")
                    await page.locator('#workflowBooking [data-workflow="new-operation"]').click()
                    await page.locator('#workflowBooking [name=client_name]').wait_for(state='visible')
                    assert await page.locator('#workflowBooking [name=client_name]').input_value() == ''
                    assert await page.locator('#workflowBooking [name=client_name]').is_enabled()
                    await page.locator('#workflowBooking [name=client_name]').fill('Next Guest')
                    await page.locator('#workflowBooking [name=room]').fill('201')
                    await page.locator('#workflowBooking [name=shoot_time]').fill('15:00')
                    await page.locator('#workflowBooking button').click()
                    await wait_async(page, "async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).filter(r=>r.kind==='booking'&&r.status==='synced').length===3;}")
                    await page.locator('#bottomNav [data-go=home]').click()
                    await page.locator('[data-action="new-booking"]').click()
                    field = page.locator('#workflowBooking [name=client_name]')
                    await field.wait_for(state='visible')
                    assert await field.is_enabled() and await field.input_value() == ''
                    assert await field.evaluate('(el)=>el===document.activeElement')
                    # Opening the shortcut again preserves an unsent next guest.
                    await field.fill('Unsent Guest')
                    await page.locator('#bottomNav [data-go=home]').click()
                    await page.locator('[data-action="new-booking"]').click()
                    assert await field.input_value() == 'Unsent Guest'
                    assert await field.is_enabled()
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.evaluate("""async()=>{
                        const m=await import('/app/js/workflow.js');await m.loadWorkflow(1);
                        document.querySelector('#app').innerHTML=m.workflowView();
                    }""")
                    await page.locator('[data-receipt-status]').get_by_role('button', name='Посмотреть фото чека').click()
                    await wait_async(page, "()=>document.querySelector('dialog[aria-label=\"Фото чека\"] img')?.naturalWidth>0")
                    await page.locator('dialog[aria-label="Фото чека"]').get_by_role('button', name='Закрыть', exact=True).click()
                    await page.locator('dialog[aria-label="Фото чека"]').wait_for(state='detached')
                    assert await page.evaluate("async()=>{for(const name of await caches.keys()){for(const request of await (await caches.open(name)).keys())if(new URL(request.url).pathname.startsWith('/api/'))return false;}return true;}")
                    # Duplicate selections can leave all original files accepted
                    # while the server correctly blocks completion (1 unique / 2).
                    # Repair in the originating browser, including after reload.
                    async with factory() as session:
                        repair_booking = await session.get(Booking, 3)
                        repair_booking.photographer_id = 2
                        repair_booking.status = 'READY_FOR_SALE'
                        repair_shooting = await session.scalar(select(Shooting).where(Shooting.booking_id == 3))
                        repair_shooting.status = 'READY_FOR_SALE'
                        await session.commit()
                    await page.evaluate("""async()=>{
                        const m=await import('/app/js/workflow.js');await m.loadWorkflow(3);m.workflowSelect(3);
                    }""")
                    repair_form = page.locator('#workflowSale')
                    await repair_form.locator('[name=total]').fill('150')
                    await repair_form.locator('[name=sold]').fill('2')
                    await repair_form.locator('[name=receipt]').set_input_files('app/webapp/assets/academy/evening.jpg')
                    await repair_form.locator('[name=selected]').set_input_files([
                        'app/webapp/assets/academy/couple.jpg', 'app/webapp/assets/academy/couple.jpg'])
                    await repair_form.locator('button').first.click()
                    try:
                        await repair_form.locator('[data-workflow="repair-sale"]').wait_for(state='visible')
                    except Exception:
                        print(await page.evaluate("""async()=>({queue:(await (await import('/app/js/outbox.js')).outboxRows()).map(r=>({kind:r.kind,status:r.status,error:r.error})),errors:document.body.innerText.slice(-1500)})"""))
                        raise
                    assert await repair_form.locator('[name=sold]').is_disabled()
                    original_repair = await page.evaluate("""async()=>{
                        const {outboxRows}=await import('/app/js/outbox.js'),rows=await outboxRows();
                        const keys=JSON.parse(document.querySelector('#workflowSale').dataset.pendingKeys);
                        return rows.filter(r=>keys.includes(r.key)).map(r=>({key:r.key,kind:r.kind,data:r.data}));
                    }""")
                    await page.reload()
                    await page.locator('#workflowSale [data-workflow="repair-sale"]').wait_for(state='visible')
                    await page.locator('#workflowSale [data-workflow="repair-sale"]').click()
                    await wait_async(page, "()=>!document.querySelector('#workflowSale [name=sold]').disabled")
                    assert await page.locator('#workflowSale [name=sold]').is_enabled()
                    await page.locator('#workflowSale [name=sold]').fill('1')
                    await page.locator('#workflowSale button').first.click()
                    await wait_async(page, """async()=>{
                        const {outboxRows}=await import('/app/js/outbox.js'),rows=await outboxRows();
                        const keys=JSON.parse(document.querySelector('#workflowSale').dataset.pendingKeys);
                        return keys.every(key=>rows.some(r=>r.key===key&&r.status==='synced'));
                    }""")
                    assert await page.evaluate("""async original=>{
                        const {outboxRows}=await import('/app/js/outbox.js'),rows=await outboxRows();
                        return original.every(old=>{const row=rows.find(r=>r.key===old.key);return row&&JSON.stringify(row.data)===JSON.stringify(old.data);});
                    }""", original_repair)
                    async with factory() as session:
                        recovered = (await session.scalars(select(Sale).where(Sale.booking_id == 3))).all()
                        assert len(recovered) == 1 and recovered[0].sold_photos == 1 and recovered[0].amount == 400
                        assert await session.scalar(select(func.count(Receipt.id)).where(Receipt.booking_id == 3)) == 1
                    output = Path(os.getenv('CALLS_QA_DIR', '/tmp/photo-boss-calls-qa'))
                    output.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(output/'offline-replay-mobile.png'), full_page=True)
                    owner = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844}, is_mobile=True)
                    await owner.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(signed(3))+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                    await owner.route('https://telegram.org/**', lambda r: r.fulfill(status=200, content_type='text/javascript', body=''))
                    panel = await owner.new_page()
                    panel.on('pageerror', lambda e: errors.append(str(e)))
                    await panel.goto(str(server.make_url('/app/#team')))
                    for purpose in ('start', 'end'):
                        form = panel.locator(f'.attendance-review[data-purpose={purpose}]')
                        await form.locator('..').locator('summary').click()
                        await form.locator('..').get_by_role('button', name='Посмотреть фото').click()
                        await form.locator('..').get_by_alt_text('Фото офлайн-отметки').wait_for()
                        value = await panel.evaluate("async purpose=>{const {api}=await import('/app/js/api.js');return (await api('/attendance/pending')).items.find(r=>r.purpose===purpose).offline_claimed_at.slice(0,16);}", purpose)
                        await form.locator('[name=verifiedAt]').fill(value)
                        await form.locator('[name=note]').fill('Checked photo and shift time')
                        await form.locator('button').click()
                        await form.wait_for(state='detached')
                    await panel.get_by_text('Нет отметок, ожидающих проверки.').wait_for()
                    async with factory() as session:
                        assert (await session.scalar(select(ShiftCheckIn))).status == 'STARTED'
                        assert (await session.scalar(select(ShiftCheckOut))).status == 'FINISHED'
                    assert not errors, errors
                    await panel.screenshot(path=str(output/'offline-attendance-review-mobile.png'), full_page=True)
                    print('PASS: cold offline start, receipt503 isolation and legacy409 recovery, lost response replay, single sale/commission, offline camera shifts and owner review, no API cache')
                finally:
                    await browser.close()
    finally:
        await server.close()
        await engine.dispose()
        temporary.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
