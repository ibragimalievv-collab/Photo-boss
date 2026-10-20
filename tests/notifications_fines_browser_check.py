"""Authenticated fixture-only mobile inbox and fine exceptions; no real push or sales."""
import asyncio
import json
import os
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from sqlalchemy import select
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.attendance import install_attendance
from app.attendance_fines import fine_key
from app.hotel_points import save_json
from app.miniapp_api import install_miniapp
from app.models import Booking, Notification, Sale, Setting, UserRole
from app.push_notifications import queue_sale_created
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.booking_reminders import queue_twenty_minute_reminders

BASE='https://photo-boss-inbox.invalid'


async def reminder_browser_flow(browser, client, factory, owner_page, errors):
    """Real browser rendering of a scheduler-created assigned shoot reminder."""
    clock = [datetime(2026, 10, 20, 9, 40, tzinfo=UTC).replace(tzinfo=None)]
    env = {'PHOTO_BOSS_NOTIFICATION_CHANNEL': 'app', 'WEB_PUSH_PUBLIC_KEY': 'test-only',
           'WEB_PUSH_PRIVATE_KEY': 'test-only', 'WEB_PUSH_SUBJECT': 'mailto:test@example.com'}
    with patch.dict(os.environ, env), patch('app.push_notifications.utc_now', lambda: clock[0]):
        async with factory() as session:
            session.add(UserRole(user_id=2, role='PHOTOGRAPHER'))
            booking = Booking(hotel_id=1, client_id=1, room='reminder-205', guest_count=2,
                shoot_date=date(2026, 10, 20), shoot_time=time(13), package_id=1,
                manager_id=1, photographer_id=2, status='ASSIGNED')
            session.add(booking)
            await session.flush()
            assert await queue_twenty_minute_reminders(session, clock[0] - timedelta(seconds=1)) == 0
            assert await queue_twenty_minute_reminders(session, clock[0]) == 1
            assert await queue_twenty_minute_reminders(session, clock[0] + timedelta(seconds=5)) == 0
            notice = await session.scalar(select(Notification).where(Notification.kind == 'booking_reminder'))
            assert notice.user_id == 2
            notice_id, bid = notice.id, booking.id
            await session.commit()
        # Even the owner cannot read or acknowledge a photographer's private inbox item.
        response = await client.post('/api/miniapp/push/read', json={'id': notice_id},
            headers={'X-Telegram-Init-Data': signed(1)})
        assert response.status == 404
        await owner_page.locator('#closeSheet').click()
        await owner_page.goto(BASE+'/app/#more')
        await owner_page.locator('[data-action=app-notifications]').click()
        assert await owner_page.locator(f'[data-notice-read="{notice_id}"]').count() == 0

        context = await browser.new_context(viewport={'width': 320, 'height': 720}, is_mobile=True, has_touch=True)
        init = signed(2)
        await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(init)+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
        async def route(request):
            if request.request.url.startswith('https://telegram.org/'):
                return await request.fulfill(status=200, content_type='text/javascript', body='')
            response = await client.request(request.request.method, request.request.url.removeprefix(BASE),
                data=request.request.post_data_buffer,
                headers={'X-Telegram-Init-Data': init,
                         'Content-Type': request.request.headers.get('content-type', 'application/json')})
            await request.fulfill(status=response.status,
                headers={'Content-Type': response.headers.get('Content-Type', 'text/plain')}, body=await response.read())
        await context.route('**/*', route)
        try:
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(BASE+'/app/#more')
            await page.wait_for_function("document.querySelector('[data-notice-count]')?.textContent==='3'")
            await page.locator('[data-action=app-notifications]').click()
            card = page.locator(f'[data-notice-read="{notice_id}"]')
            await card.wait_for(state='visible')
            await card.scroll_into_view_if_needed()
            details = await card.inner_text()
            for value in ('Скоро съёмка', f'Съёмка №{bid}', '20.10.2026 в 13:00', 'Hotel', 'reminder-205',
                          'Нет подключённого устройства'):
                assert value in details, details
            await page.screenshot(path='/tmp/photo-boss-final-qa/reminder-inbox-320.png')
            # Advancing fixture time requires no real twenty-minute wait.
            clock[0] = datetime(2026, 10, 20, 10, tzinfo=UTC).replace(tzinfo=None)
            await page.reload()
            await page.wait_for_function("document.querySelector('[data-notice-count]')?.textContent==='3'")
            await page.locator('[data-action=app-notifications]').click()
            card = page.locator(f'[data-notice-read="{notice_id}"]')
            await card.wait_for(state='visible')
            await card.scroll_into_view_if_needed()
            assert 'Время напоминания истекло; новые push не отправляются' in await card.inner_text()
            assert 'reminder-205' in await card.inner_text()
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path='/tmp/photo-boss-final-qa/reminder-expired-inbox-320.png')
        finally:
            await context.close()


async def main():
 with tempfile.TemporaryDirectory() as folder:
  engine,factory,_=await fixture('sqlite+aiosqlite:///'+folder+'/browser.db')
  async with factory() as s:
   sale,booking=await s.get(Sale,1),await s.get(Booking,1)
   await queue_sale_created(s,sale,booking);await s.commit()
  key=fine_key(2,'2026-09-21','late')
  async with engine.begin() as conn:
   await save_json(conn,key,{'userId':2,'date':'2026-09-21','kind':'late','label':'Опоздание','amount':500,'calculatedAmount':500,'ruleConfigured':True,'ruleAmount':500,'detected':True,'payrollId':None,'override':None,'expectedAt':'2026-09-21T06:00:00+00:00','observedAt':'2026-09-21T06:10:00+00:00'})
  app=web.Application();bot=SimpleNamespace(token=TOKEN,me=AsyncMock(),send_message=AsyncMock())
  api=install_miniapp(app,engine=engine,bot=bot,lessons=ACADEMY_LESSONS,blocks=ACADEMY_BLOCKS)
  original=api.rows
  async def rows(conn,sql,**params):return await original(conn,sql.replace(' FOR UPDATE',''),**params)
  api.rows=rows;install_attendance(app,api)
  client=TestClient(TestServer(app));await client.start_server();errors=[]
  try:
   async with async_playwright() as p:
    browser=await p.chromium.launch()
    try:
     context=await browser.new_context(viewport={'width':320,'height':720},is_mobile=True,has_touch=True)
     init=signed(1)
     await context.add_init_script('window.Telegram={WebApp:{initData:'+json.dumps(init)+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
     async def route(r):
      if r.request.url.startswith('https://telegram.org/'):
       return await r.fulfill(status=200,content_type='text/javascript',body='')
      response=await client.request(r.request.method,r.request.url.removeprefix(BASE),data=r.request.post_data_buffer,headers={'X-Telegram-Init-Data':init,'Content-Type':r.request.headers.get('content-type','application/json')})
      await r.fulfill(status=response.status,headers={'Content-Type':response.headers.get('Content-Type','text/plain')},body=await response.read())
     await context.route('**/*',route)
     page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
     await page.goto(BASE+'/app/#more')
     await page.wait_for_function("document.querySelector('[data-notice-count]')?.textContent==='1'")
     async with factory() as s:
      sale=Sale(booking_id=1,created_by_id=1,credited_user_id=2,sold_photos=1,amount=400,percent=0,commission=0)
      s.add(sale);await s.flush();await queue_sale_created(s,sale,await s.get(Booking,1));await s.commit()
     await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
     await page.wait_for_function("document.querySelector('[data-notice-count]')?.textContent==='2'")
     await page.locator('[data-action=app-notifications]').click()
     await page.locator('[data-notice-read]').first.click()
     await page.wait_for_function("document.querySelector('[data-notice-count]')?.textContent==='1'")
     await page.goto(BASE+'/app/#more')
     await page.get_by_role('button',name='Штрафы и исключения',exact=True).click()
     await page.locator('.attendance-fine-edit').locator('..').locator('summary').click()
     await page.locator('.attendance-fine-edit [name=amount]').fill('0')
     await page.locator('.attendance-fine-edit [name=reason]').fill('Уважительная причина проверена владельцем')
     async with page.expect_response(lambda r:r.url.endswith('/attendance/fines') and r.request.method=='PUT' and r.status==200):
      await page.get_by_role('button',name='Сохранить исключение',exact=True).click()
     await page.get_by_text('Исправлено: Уважительная причина проверена владельцем',exact=True).wait_for(state='attached')
     async with factory() as s:assert json.loads((await s.get(Setting,key)).value)['amount']==0
     assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
     assert not errors,errors
     Path('/tmp/photo-boss-final-qa').mkdir(exist_ok=True)
     await page.screenshot(path='/tmp/photo-boss-final-qa/fines-inbox-320.png',full_page=True)
     await reminder_browser_flow(browser, client, factory, page, errors)
     assert not errors, errors
     print('PASS: sale-created event, assigned photographer reminder scheduler and browser inbox, private recipients, expiry preserving details, fine exception save and audit; 320px no overflow')
    finally:await browser.close()
  finally:await client.close();await engine.dispose()
if __name__=='__main__':asyncio.run(main())
