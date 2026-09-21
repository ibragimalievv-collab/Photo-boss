"""Mobile UI and IndexedDB replay against fixture APIs; no external accounts."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.academy_practice import install_academy_practice
from app.attendance import install_attendance
from app.miniapp_api import install_miniapp
from app.people import install_people
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.work_calls import install_work_calls
from app.work_chat import install_work_chat

BASE='https://photo-boss-update.invalid'


async def main():
    engine,_factory,_api=await fixture()
    bot=SimpleNamespace(token=TOKEN,send_message=AsyncMock(),me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app=web.Application(client_max_size=25*1024*1024)
    api=install_miniapp(app,engine=engine,bot=bot,lessons=ACADEMY_LESSONS,blocks=ACADEMY_BLOCKS)
    original=api.rows
    async def rows(conn,sql,**params): return await original(conn,sql.replace(' FOR UPDATE',''),**params)
    api.rows=rows
    install_attendance(app,api);install_people(app,api)
    chat=install_work_chat(app,api);install_work_calls(app,chat);install_academy_practice(app,api)
    client=TestClient(TestServer(app));await client.start_server()
    output=Path(os.getenv('CALLS_QA_DIR','/tmp/photo-boss-update-qa'));output.mkdir(parents=True,exist_ok=True)
    offline=False
    errors=[]
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
            try:
                context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                init=signed(1)
                await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(init)+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                async def route(r):
                    url=r.request.url
                    if url.startswith('https://telegram.org/'):
                        return await r.fulfill(status=200,content_type='text/javascript',body='')
                    path=url.removeprefix(BASE)
                    if offline and path.startswith('/api/'):
                        return await r.abort('internetdisconnected')
                    response=await client.request(r.request.method,path,data=r.request.post_data_buffer,headers={'X-Telegram-Init-Data':init,'Content-Type':r.request.headers.get('content-type','application/json')})
                    await r.fulfill(status=response.status,headers={'Content-Type':response.headers.get('Content-Type','text/plain')},body=await response.read())
                await context.route('**/*',route)
                page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                for name,title in [('insights','Контроль бизнеса'),('team','Команда и найм'),('onboarding','Начало работы с Photo Boss'),('workday','Итоги смены'),('development','Развитие фотографа и продажи')]:
                    await page.goto(BASE+'/app/#'+name)
                    await page.get_by_role('heading',name=title,exact=True).wait_for()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),name
                    await page.screenshot(path=str(output/f'update-{name}-mobile.png'),full_page=True)
                await page.goto(BASE+'/app/#workday')
                await page.get_by_text('Настроить чек-лист',exact=True).click()
                await page.locator('#checklistForm [name=title]').fill('Проверить резервную карту')
                await page.locator('#checklistForm [value=OWNER]').check()
                await page.locator('#checklistForm button').click()
                await page.get_by_text('Проверить резервную карту · не отмечено',exact=True).wait_for()
                offline=True;await context.set_offline(True)
                await page.get_by_role('button',name='Готово',exact=True).click()
                await page.get_by_text('Сохранено локально',exact=True).wait_for()
                offline=False;await context.set_offline(False)
                await page.get_by_text('Синхронизировано',exact=True).wait_for()
                await page.reload()
                await page.get_by_text('Проверить резервную карту · выполнено',exact=True).wait_for()
                assert not errors,errors
                print('Mobile routes, no overflow, real IndexedDB queue and replay: PASS')
            finally: await browser.close()
    finally:
        await client.close();await engine.dispose()


if __name__=='__main__': asyncio.run(main())
