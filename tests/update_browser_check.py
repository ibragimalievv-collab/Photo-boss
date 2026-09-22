"""Mobile UI and IndexedDB replay against fixture APIs; no external accounts."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from test_insights import fixture
from test_miniapp_release import TOKEN, signed
from work_calls_browser_check import wait_async

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
    ai_config=patch('app.development.config',SimpleNamespace(openai_api_key='fixture-key'))
    ai_reply=patch('app.development.ai.roleplay',AsyncMock(return_value={'status':'completed','data':{'reply':'Объясните стоимость до выбора кадров.','score':80,'errors':['Цена названа поздно'],'recommendations':['Назовите условия заранее']}}))
    ai_config.start();ai_reply.start()
    offline=False
    errors=[]
    failure_mode=None
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
            try:
                context=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                init=signed(1)
                await context.add_init_script('window.__externalOpen=[];window.open=(url)=>window.__externalOpen.push(url);window.Telegram={WebApp:{initData:'+json.dumps(init)+',openTelegramLink(url){window.__externalOpen.push(url);},ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                async def route(r):
                    url=r.request.url
                    if url.startswith('https://telegram.org/'):
                        return await r.fulfill(status=200,content_type='text/javascript',body='')
                    path=url.removeprefix(BASE)
                    if failure_mode and path=='/api/miniapp/me':
                        return await r.fulfill(status=failure_mode,content_type='application/json',body=json.dumps({'error':'Проверка ошибки входа','telegramId':1} if failure_mode==403 else {'error':'Проверка ошибки загрузки'}))
                    if offline and path.startswith('/api/'):
                        return await r.abort('internetdisconnected')
                    response=await client.request(r.request.method,path,data=r.request.post_data_buffer,headers={'X-Telegram-Init-Data':init,'Content-Type':r.request.headers.get('content-type','application/json')})
                    await r.fulfill(status=response.status,headers={'Content-Type':response.headers.get('Content-Type','text/plain')},body=await response.read())
                await context.route('**/*',route)
                page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                for name,title in [('insights','Контроль бизнеса'),('team','Команда и найм'),('onboarding','Начало работы с Photo Boss'),('workday','Итоги смены'),('development','Развитие фотографа и продажи')]:
                    await page.goto(BASE+'/app/#'+name)
                    await page.get_by_role('heading',name=title,exact=True).wait_for()
                    if name == 'insights':
                        await page.get_by_text('Оплаченные расходы и выплаты',exact=True).click()
                        await page.locator('#cashExpense [name=category]').select_option('OTHER')
                        await page.locator('#cashExpense [name=amount]').fill('12.34')
                        await page.locator('#cashExpense [name=note]').fill('Paid supplies fixture')
                        await page.locator('#cashExpense [name=paidConfirmed]').check()
                        async with page.expect_response(lambda r:r.url.endswith('/operations/sync') and r.status==200):
                            await page.locator('#cashExpense button').click()
                        await wait_async(page,"async ()=>(await (await import('/app/js/outbox.js')).outboxRows()).some(r=>r.kind==='cash_expense'&&r.status==='synced')")
                        await page.get_by_role('button',name='Обновить суммы',exact=True).click()
                        await page.get_by_text('Paid supplies fixture',exact=True).wait_for(state='attached')
                        await page.get_by_text('Оплаченные расходы и выплаты',exact=True).click()
                        await page.get_by_text('Paid supplies fixture',exact=True).wait_for()
                        await page.screenshot(path=str(output/'update-cash-mobile.png'),full_page=True)
                        await page.get_by_text('Отменить ошибочную запись',exact=True).click()
                        await page.locator('.cashVoid [name=reason]').fill('Correct erroneous fixture entry')
                        async with page.expect_response(lambda r:r.url.endswith('/void') and r.status==200):
                            await page.locator('.cashVoid button').click()
                        await page.get_by_text('Основание отмены: Correct erroneous fixture entry',exact=True).wait_for(state='attached')
                        await page.get_by_text('Оплаченные расходы и выплаты',exact=True).click()
                        await page.get_by_text('Основание отмены: Correct erroneous fixture entry',exact=True).wait_for()
                        await page.locator('#notifyDaily').check()
                        async with page.expect_response(lambda r:r.url.endswith('/events/preferences') and r.status==200):
                            await page.get_by_role('button',name='Сохранить уведомления',exact=True).click()
                        await page.reload()
                        await page.get_by_role('heading',name=title,exact=True).wait_for()
                        assert await page.locator('#notifyDaily').is_checked()
                    if name == 'team':
                        await page.get_by_text('Дисциплина команды',exact=True).click()
                        await page.locator('#disciplinePeriod [name=from]').fill('2026-09-14')
                        await page.locator('#disciplinePeriod [name=to]').fill('2026-09-20')
                        await page.locator('#disciplinePeriod button').click()
                        await page.get_by_text('2026-09-14 — 2026-09-20; сравнение с 2026-09-07 — 2026-09-13',exact=True).wait_for()
                        await page.get_by_role('button',name='Photo',exact=True).last.click()
                        await page.get_by_role('heading',name='Photo',exact=True).wait_for()
                        await page.get_by_text('История записей и съёмок',exact=True).click()
                    if name == 'development':
                        await page.get_by_role('button',name='Тренироваться с AI',exact=True).first.click()
                        await page.locator('#salesTrainingForm textarea').fill('Съёмка бесплатная, выбранный кадр стоит 400 рублей.')
                        async with page.expect_response(lambda r:r.url.endswith('/turn') and r.status==200):
                            await page.get_by_role('button',name='Завершить и получить оценку',exact=True).click()
                        await page.get_by_role('heading',name='Оценка 80/100',exact=True).wait_for()
                        await page.reload()
                        await page.get_by_role('button',name='№1 · budget · завершена · 80/100',exact=True).click()
                        await page.get_by_role('heading',name='Оценка 80/100',exact=True).wait_for()
                    if name == 'onboarding':
                        for field in ('price','receipt','sync','flag'):
                            await page.locator(f'#entryQuiz [name={field}][value="1"]').check()
                        await page.locator('#entryQuiz button').click()
                        await page.get_by_text('Результат 100/100 · зачтено',exact=True).wait_for()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),name
                    await page.screenshot(path=str(output/f'update-{name}-mobile.png'),full_page=True)
                # Every work shortcut and recovery path stays in the Mini App.
                await page.goto(BASE+'/app/')
                await page.locator('[data-action="new-booking"]').click()
                await page.locator('#workflowBooking [name=client_name]').wait_for(state='visible')
                assert await page.locator('#workflowBooking [name=client_name]').evaluate('(e)=>e===document.activeElement')
                await page.goto(BASE+'/app/')
                await page.locator('[data-action="new-sale"]').click()
                await page.locator('#workflowSalePick').wait_for(state='visible')
                await page.goto(BASE+'/app/')
                await page.locator('#attendanceCard button').click()
                await page.locator('#attendanceDialog').wait_for(state='visible')
                await page.locator('[data-att=close]').click()
                await page.locator('[data-open-people]').click()
                await page.locator('[data-people=hotels]').click()
                await page.locator('#pbHotelForm [name=name]').fill('Hotel from mobile app')
                await page.locator('#pbHotelForm button[type=submit]').click()
                await page.get_by_text('Hotel from mobile app',exact=True).wait_for()
                await page.locator('[data-people=list]').click()
                await page.locator('[data-people=add]').click()
                assert 'командой /myid' not in await page.locator('#pbEmployeeForm').inner_text()
                assert 'Hotel from mobile app' in await page.locator('#pbEmployeeForm').inner_text()
                await page.screenshot(path=str(output/'update-in-app-staff-mobile.png'),full_page=True)
                await page.locator('[data-people=close]').click()
                for status_code,title in [(503,'Не удалось загрузить'),(401,'Требуется повторный вход'),(403,'Рабочий доступ недоступен')]:
                    failure_mode=status_code
                    await page.reload()
                    await page.get_by_role('heading',name=title,exact=True).wait_for()
                    assert await page.locator('#attendanceCard').count()==0
                    assert await page.locator('a[href*="t.me"],a[href^="tg:"]').count()==0
                    assert 'в боте' not in await page.locator('#app').inner_text()
                    assert await page.evaluate('window.__externalOpen.length')==0
                    if status_code==403:
                        assert 'Ваш Telegram ID: 1' in await page.locator('#app').inner_text()
                    await page.screenshot(path=str(output/f'update-in-app-error-{status_code}.png'),full_page=True)
                    failure_mode=None
                    await page.get_by_role('button',name='Повторить',exact=True).click()
                    await page.get_by_role('heading',name='Всё под контролем',exact=True).wait_for()
                assert bot.send_message.await_count==0
                assert bot.me.await_count==0
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
        ai_reply.stop();ai_config.stop()
        await client.close();await engine.dispose()


if __name__=='__main__': asyncio.run(main())
