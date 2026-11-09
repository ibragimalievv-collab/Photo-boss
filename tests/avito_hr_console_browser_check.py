"""Browser UI fixture; no Avito, production login, or external message calls."""
import asyncio
import os
from pathlib import Path

from aiohttp import web


async def main():
    from playwright.async_api import async_playwright

    fields = ['title', 'duties', 'pay', 'payout', 'schedule', 'location', 'housing',
              'travel', 'training', 'equipment', 'contract', 'bonuses']
    state = {'configured': True, 'workerReady': True, 'lastCheck': 1, 'fields': fields,
             'vacancies': [{'code': 'test', 'payload': {'title': 'Тестовая вакансия', 'pay': '15%',
                           'schedule': '3/1', 'location': 'Анапа'}, 'revision': 0, 'confirmed': False}],
             'candidates': [{'chat_id': 'test-chat', 'vacancy_code': 'test', 'status': 'ACTIVE',
                             'profile': {'name': '<img src=x onerror=alert(1)>'},
                             'transcript': [{'direction': 'in', 'text': '<script>alert(1)</script>'}]}],
             'profileLabels': {'name': 'Имя'}, 'note': 'Тестовый диалог'}
    calls = []

    async def handler(request):
        path = request.path
        if path == '/app/js/api.js':
            return web.Response(content_type='text/javascript', text="""
export const api=async(path,options={})=>{
  const r=await fetch('/api/miniapp'+path,{method:options.method||'GET',
    headers:{'Content-Type':'application/json'},body:options.body?JSON.stringify(options.body):undefined});
  const value=await r.json();if(!r.ok)throw new Error(value.error||'Ошибка');return value;
};""")
        if path.startswith('/api/miniapp/'):
            payload = await request.json() if request.can_read_body else None
            calls.append((path, payload))
            result = state
            if path.endswith('/terms'):
                state['vacancies'][0].update(payload, revision=1)
                result = {'ok': True, 'revision': 1}
            elif path.endswith('/preview'):
                result = {'answer': 'Тестовый ответ по условиям.', 'needsManager': False, 'sent': False}
            elif path.endswith('/control'):
                state['candidates'][0]['status'] = 'HUMAN' if payload['action'] == 'pause' else 'ACTIVE'
                result = {'ok': True}
            return web.json_response(result)
        if path == '/app/css/styles.css':
            return web.Response(content_type='text/css', text='')
        files = {'/app/hr-avito.html': 'app/webapp/hr-avito.html',
                 '/app/js/hr-avito.js': 'app/webapp/js/hr-avito.js'}
        if path not in files:
            raise web.HTTPNotFound()
        return web.FileResponse(files[path])

    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page(viewport={'width': 390, 'height': 844})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/app/hr-avito.html')
                await page.get_by_role('heading', name='Кандидаты', exact=True).wait_for()
                assert await page.locator('img,script:not([src])').count() == 0
                details = page.locator('details.panel').first
                await details.locator('summary').click()
                await details.get_by_role('checkbox').check()
                await details.get_by_role('button', name='Сохранить условия').click()
                await page.wait_for_function("document.querySelector('#hr-status').textContent==='Условия сохранены.'")
                assert state['vacancies'][0]['confirmed'] is True
                await details.locator('summary').click()
                await details.get_by_placeholder('Тестовый вопрос кандидата').fill('Какая зарплата?')
                await details.get_by_role('button', name='Проверить', exact=True).click()
                await page.get_by_text('Сообщение не отправлялось.', exact=False).wait_for()
                await page.get_by_role('button', name='Взять переписку на себя').click()
                await page.get_by_role('button', name='Вернуть ассистенту').wait_for()
                assert state['candidates'][0]['status'] == 'HUMAN'
                await page.get_by_role('button', name='Вернуть ассистенту').click()
                await page.get_by_role('button', name='Взять переписку на себя').wait_for()
                assert state['candidates'][0]['status'] == 'ACTIVE'
                assert not errors
                assert len([path for path, _ in calls if path.endswith('/preview')]) == 1
                evidence = Path(os.getenv('CALLS_QA_DIR', '/tmp/photo-boss-avito-qa'))
                evidence.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(evidence / 'avito-hr-console.png'), full_page=True)
                print('PASS: HR console mobile UI, save conditions, preview, pause/resume and safe candidate text')
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    # Version 3.62 from Timeweb ships the Avito worker but not its HR console
    # (same reason the Avito dialogue tests use importorskip).
    if not Path('app/webapp/hr-avito.html').exists():
        print('SKIP: HR console is not part of this version (app/webapp/hr-avito.html is absent)')
    else:
        asyncio.run(main())
