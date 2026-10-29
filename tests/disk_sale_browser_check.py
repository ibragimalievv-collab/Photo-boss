"""Three photo sources and a lost Disk-selection acknowledgement in a real browser."""
import asyncio
import json
import os
import ssl
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from _pytest.monkeypatch import MonkeyPatch
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from test_disk_sale_media import prepared
from test_miniapp_release import TOKEN, signed
from work_calls_browser_check import wait_async

from app.disk_sale_media import BINDING
from app.miniapp_api import install_miniapp
from app.models import PayrollEntry, Sale, SaleDraft, SaleDraftPhoto, Setting
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS


async def reveal_disk_source(form):
    for selector in ('.sale-details', '.sale-extra-source'):
        details = form.locator(selector)
        if not await details.evaluate('element=>element.open'):
            await details.locator(':scope > summary').click()


async def main():
    with tempfile.TemporaryDirectory(prefix='disk-sale-browser-') as folder, MonkeyPatch.context() as monkey:
        engine,factory,_,request,_,disk,did = await prepared(monkey,'sqlite+aiosqlite:///'+folder+'/test.db')
        bid=int(request.match_info['id'])
        disk.folders['disk:/']=[{'type':'dir','name':'Hotel','path':'disk:/Hotel'}]
        monkey.setattr('app.disk_folder_audit.read_storage',lambda:disk)
        async with factory() as session:
            draft=await session.get(SaleDraft,did)
            draft.created_by_id=2
            await session.delete(await session.get(Setting,BINDING+str(bid)))
            await session.commit()
        bot=SimpleNamespace(token=TOKEN,send_message=AsyncMock(),me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
        app=web.Application(client_max_size=25*1024*1024)
        api=install_miniapp(app,engine=engine,bot=bot,lessons=ACADEMY_LESSONS,blocks=ACADEMY_BLOCKS)
        original=api.rows
        async def rows(conn,sql,**params): return await original(conn,sql.replace(' FOR UPDATE',''),**params)
        api.rows=rows
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(os.environ['WORKFLOW_TEST_CERT'],os.environ['WORKFLOW_TEST_KEY'])
        server=TestServer(app)
        await server.start_server(ssl=tls)
        errors=[]
        try:
            async with async_playwright() as p:
                browser=await p.chromium.launch()
                try:
                    context=await browser.new_context(ignore_https_errors=True,viewport={'width':390,'height':844})
                    await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(signed(1))+'}}')
                    await context.route('https://telegram.org/**',lambda r:r.fulfill(body='',content_type='text/javascript'))
                    await context.route('https://disk.yandex.ru/**',lambda r:r.fulfill(
                        body='<title>Disk window fixture</title><p>Fixture only</p>',content_type='text/html'))
                    for prefix in ('shift','people','work-chat'):
                        await context.route(f'**/{prefix}/**',lambda r:r.fulfill(body='{}',content_type='application/json'))
                    lost=False
                    async def lose_once(route):
                        nonlocal lost
                        if route.request.post_data_json.get('kind')=='sale_disk_selected' and not lost:
                            result=await route.fetch()
                            assert result.status==200
                            lost=True
                            await route.abort('connectionreset')
                        else: await route.continue_()
                    await context.route('**/api/miniapp/operations/sync',lose_once)
                    folder_requests=0
                    async def lose_first_listing(route):
                        nonlocal folder_requests
                        folder_requests+=1
                        if folder_requests==1: await route.abort('connectionreset')
                        else: await route.continue_()
                    await context.route('**/api/miniapp/storage/disk-folders?*',lose_first_listing)
                    page=await context.new_page()
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    await page.goto(str(server.make_url('/app/#workflow')))
                    await page.locator('#workflowSalePick').select_option(str(bid))
                    await page.get_by_role('button',name='Продолжить продажу как владелец',exact=True).click()
                    form=page.locator('#workflowSale')
                    await reveal_disk_source(form)
                    for label in ['Загрузить с устройства','Прикрепить ссылку','Выбрать на Яндекс.Диске']:
                        await expect(form.get_by_role('radio',name=label,exact=True)).to_be_visible()
                    await form.get_by_role('radio',name='Прикрепить ссылку',exact=True).check()
                    await expect(form.locator('[name=diskSource]')).to_be_visible()
                    assert folder_requests==0,'Link mode must not browse the private Disk'
                    async with context.expect_page(timeout=5000) as opened:
                        await form.get_by_role('radio',name='Выбрать на Яндекс.Диске',exact=True).check()
                    disk_window=await opened.value
                    await disk_window.wait_for_load_state()
                    assert disk_window.url=='https://disk.yandex.ru/client/disk'
                    assert await disk_window.evaluate('window.opener===null')
                    await disk_window.close()
                    await expect(form.locator('[name=diskSource]')).to_be_visible()
                    await expect(form.get_by_role('link',name='Открыть Яндекс.Диск',exact=True)).to_be_visible()
                    await form.get_by_role('button',name='Повторить открытие',exact=True).click(timeout=5000)
                    await page.reload()
                    await reveal_disk_source(form)
                    await expect(form.get_by_role('radio',name='Выбрать на Яндекс.Диске',exact=True)).to_be_checked()
                    await expect(form.get_by_role('button',name='📁 Hotel',exact=True)).to_be_visible()
                    assert len(context.pages)==1,'Form recovery must not open a new Disk window'
                    await form.get_by_role('button',name='📁 Hotel',exact=True).click(timeout=5000)
                    async with factory() as session:
                        assert await session.get(Setting,BINDING+str(bid)) is None,'Browsing must not bind a folder'
                    await form.get_by_role('button',name='Привязать эту папку к продаже',exact=True).click()
                    await form.locator('[data-disk-photo="disk:/Hotel/a.jpg"]').check(timeout=5000)
                    failed_files=False
                    async def lose_first_files(route):
                        nonlocal failed_files
                        if not failed_files:
                            failed_files=True
                            await route.abort('connectionreset')
                        else: await route.continue_()
                    await context.route('**/api/miniapp/bookings/*/disk-files?*',lose_first_files)
                    await form.get_by_role('button',name='📁 Family',exact=True).click()
                    await form.get_by_role('button',name='Повторить открытие',exact=True).click()
                    await form.locator('[data-disk-photo="disk:/Hotel/Family/b.jpg"]').check()
                    await expect(form.locator('[data-sale-disk-selection]')).to_contain_text('Выбрано на Диске: 2')
                    waiting,release=asyncio.Event(),asyncio.Event()
                    async def delayed_listing(route):
                        waiting.set()
                        await release.wait()
                        await route.continue_()
                    await context.route('**/api/miniapp/storage/disk-folders?*',delayed_listing)
                    await form.get_by_role('button',name='Выбрать папку рабочего Диска',exact=True).click()
                    await asyncio.wait_for(waiting.wait(),timeout=5)
                    await form.get_by_role('radio',name='Загрузить с устройства',exact=True).check()
                    release.set()
                    await expect(form.locator('[data-disk-source]')).to_be_hidden()
                    async with context.expect_page(timeout=5000) as opened:
                        await form.get_by_role('radio',name='Выбрать на Яндекс.Диске',exact=True).check()
                    await (await opened.value).close()
                    await expect(form.locator('[data-disk-photo="disk:/Hotel/a.jpg"]')).to_be_checked()
                    await expect(form.locator('[data-sale-disk-folders]')).to_be_empty()
                    await expect(form.locator('[data-sale-disk-selection]')).to_contain_text('Выбрано на Диске: 2')
                    await form.get_by_role('button',name='Сохранить продажу',exact=True).click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_disk_selected'&&r.status==='local'&&r.error);}")
                    await page.get_by_role('button',name='Синхронизировать сейчас',exact=True).click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_complete'&&r.status==='synced');}")
                    async with factory() as session:
                        sales=list(await session.scalars(select(Sale)))
                        assert len(sales)==1 and sales[0].amount==800 and sales[0].sold_photos==2
                        assert await session.scalar(select(func.count(SaleDraftPhoto.id)))==2
                        assert await session.scalar(select(func.count(PayrollEntry.id)))==1
                    assert lost and failed_files and folder_requests>=3 and not errors,errors
                    disk.upload_bytes.assert_not_awaited()
                    disk.delete.assert_not_awaited()
                    output=Path('/tmp/photo-boss-calls-qa');output.mkdir(exist_ok=True,parents=True)
                    await page.screenshot(path=str(output/'disk-sale-three-sources.png'),full_page=True)
                    print('PASS: real external Disk window on user selection, no opener, no popup on recovery; in-app folders and retries retain selected photos; stale response ignored; one sale and commission after lost response')
                except Exception as exc:
                    print('ORIGINAL ERROR:',str(exc))
                    print('BROWSER DIAGNOSTICS:', await page.locator('body').inner_text(), 'JS ERRORS:', errors)
                    raise
                finally: await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__=='__main__': asyncio.run(main())
