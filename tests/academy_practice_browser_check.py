"""Real app UI and HTTP practice API, isolated staff and image storage."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright, expect
from test_academy_practice import PracticeTests
from test_miniapp_release import signed

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://practice-photo-boss.invalid"


async def main():
    fixture = PracticeTests()
    await fixture.asyncSetUp()
    fixture.finish_lessons()
    errors = []
    plan = {'title': 'AI-практика света', 'focus': 'light', 'reason': 'Диагностика',
            'lessonSlugs': ['light'], 'category': 'woman', 'scenario': '',
            'instructions': ['Выберите свет', 'Снимите разные планы', 'Проверьте лица'],
            'successCriteria': ['Пять разных кадров', 'Резкость на глазах']}
    coach_config = patch('app.academy_coach.config', SimpleNamespace(openai_api_key='fixture'))
    coach_provider = patch('app.academy_coach.structured', AsyncMock(return_value={'status': 'completed', 'data': plan}))
    coach_config.start(); coach_provider.start()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            async def route(r):
                if r.request.url.startswith('https://telegram.org/') or any(
                    r.request.url.startswith(BASE + prefix) for prefix in ('/shift/', '/people/', '/work-chat/')):
                    # Isolate unrelated widgets; keep the real Academy navigation/app module.
                    return await r.fulfill(body='', content_type='text/javascript')
                full_path = r.request.url.removeprefix(BASE)
                path = full_path.split("?", 1)[0]
                if path.startswith("/api/miniapp/"):
                    headers = {"X-Telegram-Init-Data": r.request.headers.get("x-telegram-init-data", ""),
                               "Content-Type": r.request.headers.get("content-type", "application/json")}
                    response = await fixture.client.request(r.request.method, full_path,
                        data=r.request.post_data_buffer, headers=headers)
                    return await r.fulfill(status=response.status, body=await response.read(),
                                           content_type=response.content_type)
                if path == "/app/":
                    return await r.fulfill(content_type="text/html", body=(ROOT / 'app/webapp/index.html').read_text())
                file = ROOT / "app/webapp" / path.removeprefix("/app/")
                return await r.fulfill(body=file.read_bytes(),
                    content_type="text/css" if path.endswith(".css") else "image/jpeg" if path.endswith('.jpg') else "text/javascript")

            async def page_for(uid, width):
                context = await browser.new_context(viewport={"width": width, "height": 844})
                await context.add_init_script("window.Telegram={WebApp:{initData:"+json.dumps(signed(uid+1000))+"}}")
                page = await context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.route("**/*", route)
                await page.goto(BASE + '/app/#academy')
                if uid == 1:
                    await page.locator('.academy-tabs [data-academy-tab="team"]').click()
                    await page.locator('[data-action="practice"]').click()
                else:
                    await page.locator('[data-academy-tab="coach"]').first.click()
                    await page.locator('[data-coach-practice="woman"]').click()
                return page

            trainee = await page_for(3, 320)
            await trainee.locator('[data-practice-upload="1"]').wait_for()
            for index in range(1, 6):
                await trainee.locator(f'[data-practice-upload="{index}"]').set_input_files(
                    str(ROOT / f"app/assets/training/woman/{index:02d}.jpg"))
                if index < 5:
                    await trainee.get_by_text(f"Загружено {index}/5.", exact=False).wait_for()
            await expect(trainee.locator('[data-practice-status]')).to_have_text('На проверке')
            assert len(fixture.files) == 5
            assert await trainee.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
            await fixture.practice.process_pending(fixture.storage)
            owner = await page_for(1, 390)
            await owner.locator('[data-practice-open]').first.click()
            assert await owner.locator('#practiceReview').count() == 0
            await owner.locator('[data-practice-retry]').click()
            await expect(owner.locator('.practice-body')).to_contain_text('ИИ проверяет фотографии')
            revision = {'status': 'completed', 'review': {'decision': 'REVISION', 'score': 75,
                        'reshoot_indexes': [2], 'issues': ['Другой ракурс'], 'strengths': [],
                        'next_action': 'Переснимите второй кадр'}}
            with patch('app.academy_practice.config', SimpleNamespace(openai_api_key='fixture')), \
                    patch('app.academy_practice.analyze_training_set', AsyncMock(return_value=revision)):
                await fixture.practice.process_pending(fixture.storage)
            await trainee.locator('[data-practice-refresh]').click()
            await trainee.locator('[data-practice-upload="2"]').wait_for()
            assert await trainee.locator('[data-practice-upload]').count() == 1
            await trainee.locator('[data-practice-upload="2"]').set_input_files(
                str(ROOT / 'app/assets/training/child/02.jpg'))
            await expect(trainee.locator('[data-practice-status]')).to_have_text('На проверке')
            await owner.locator('[data-practice-refresh]').click()
            accepted = {'status': 'completed', 'review': {'decision': 'ACCEPT', 'score': 90,
                        'reshoot_indexes': [], 'issues': [], 'strengths': ['Разные планы'],
                        'next_action': 'Продолжить обучение'}}
            with patch('app.academy_practice.config', SimpleNamespace(openai_api_key='fixture')), \
                    patch('app.academy_practice.analyze_training_set', AsyncMock(return_value=accepted)):
                await fixture.practice.process_pending(fixture.storage)
            await owner.locator('[data-practice-refresh]').click()
            await expect(owner.locator('[data-practice-status]')).to_have_text('Практика принята')
            await expect(trainee.locator('[data-practice-status]')).to_have_text('Практика принята', timeout=10000)
            assert fixture.bot.send_message.await_count == 0
            assert not errors, errors
            assert trainee.url == BASE + '/app/#academy' and owner.url == BASE + '/app/#academy'
            await trainee.screenshot(path='/tmp/photo-boss-calls-qa/academy-practice.png')
            print(json.dumps({'ok': True, 'fivePhotos': True, 'aiReview': True, 'manualReviewDisabled': True,
                              'reshoot': True, 'resultInApp': True, 'botHandoffs': 0, 'pageErrors': errors}))
        finally:
            await browser.close()
            await fixture.asyncTearDown()
            coach_config.stop(); coach_provider.stop()


if __name__ == '__main__':
    asyncio.run(main())

