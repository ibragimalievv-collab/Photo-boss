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

from app.miniapp_api import install_miniapp
from app.models import PayrollEntry, Sale, SaleDraftPhoto
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS


async def main():
    with tempfile.TemporaryDirectory(prefix='disk-sale-browser-') as folder, MonkeyPatch.context() as monkey:
        engine,factory,_,request,_,disk,_ = await prepared(monkey,'sqlite+aiosqlite:///'+folder+'/test.db')
        bid=int(request.match_info['id'])
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
                    page=await context.new_page()
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    await page.goto(str(server.make_url('/app/#workflow')))
                    await page.locator('#workflowSalePick').select_option(str(bid))
                    form=page.locator('#workflowSale')
                    for label in ['Загрузить с устройства','Прикрепить ссылку','Выбрать на Яндекс.Диске']:
                        await expect(form.get_by_role('radio',name=label,exact=True)).to_be_visible()
                    await form.get_by_role('radio',name='Выбрать на Яндекс.Диске',exact=True).check()
                    await form.locator('[data-disk-photo="disk:/Hotel/a.jpg"]').check(timeout=5000)
                    await form.get_by_role('button',name='📁 Family',exact=True).click()
                    await form.locator('[data-disk-photo="disk:/Hotel/Family/b.jpg"]').check()
                    await expect(form.locator('[data-sale-disk-selection]')).to_contain_text('Выбрано на Диске: 2')
                    await form.get_by_role('button',name='Сохранить продажу и фотографии',exact=True).click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_disk_selected'&&r.status==='local'&&r.error);}")
                    await page.get_by_role('button',name='Синхронизировать сейчас',exact=True).click()
                    await wait_async(page,"async()=>{const m=await import('/app/js/outbox.js');return (await m.outboxRows()).some(r=>r.kind==='sale_complete'&&r.status==='synced');}")
                    async with factory() as session:
                        sales=list(await session.scalars(select(Sale)))
                        assert len(sales)==1 and sales[0].amount==800 and sales[0].sold_photos==2
                        assert await session.scalar(select(func.count(SaleDraftPhoto.id)))==2
                        assert await session.scalar(select(func.count(PayrollEntry.id)))==1
                    assert lost and not errors,errors
                    disk.upload_bytes.assert_not_awaited()
                    disk.delete.assert_not_awaited()
                    output=Path('/tmp/photo-boss-calls-qa');output.mkdir(exist_ok=True,parents=True)
                    await page.screenshot(path=str(output/'disk-sale-three-sources.png'),full_page=True)
                    print('PASS: three photo sources; nested private folder selection; original reads only; lost response replay; one 800-RUB sale and one commission')
                except Exception as exc:
                    print('ORIGINAL ERROR:',str(exc))
                    print('BROWSER DIAGNOSTICS:', await page.locator('body').inner_text(), 'JS ERRORS:', errors)
                    raise
                finally: await browser.close()
        finally:
            await server.close()
            await engine.dispose()


if __name__=='__main__': asyncio.run(main())
