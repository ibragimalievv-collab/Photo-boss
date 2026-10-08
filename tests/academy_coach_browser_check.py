"""Mobile Academy flows using the real UI, isolated AI replies and no guest messages."""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.services.academy_curriculum import (
    BOOKING_SCENARIOS,
    MANAGER_LESSONS,
    grade_quiz,
    public_quiz,
    quiz_for,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://academy.example'


async def main():
    errors = []
    completed = set()
    session = None
    plan_calls = []
    photo_lessons = [{'slug': l.slug, 'title': l.title, 'body': l.body, 'day': l.day, 'block': l.block,
                      'locked': False, 'duration': 'Короткий урок'} for l in ACADEMY_LESSONS]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={'width': 360, 'height': 844})
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))

        async def intercept(route):
            nonlocal session
            url = urlsplit(route.request.url)
            path = url.path
            if url.netloc != 'academy.example' or path.startswith(('/shift/', '/people/', '/work-chat/')):
                return await route.fulfill(body='', content_type='application/javascript')
            if path.startswith('/app/'):
                f = ROOT / 'app/webapp' / (path[5:] or 'index.html')
                return await route.fulfill(body=f.read_bytes() if f.is_file() else b'',
                                          content_type=mimetypes.guess_type(f.name)[0] or 'text/plain')
            endpoint = path.removeprefix('/api/miniapp')
            body = json.loads(route.request.post_data or '{}')
            booking = 'track=booking' in url.query
            data = {}
            if endpoint == '/me':
                data = {'user': {'id': 3, 'name': 'Учебный фотограф', 'roles': ['PHOTOGRAPHER'], 'theme': 'dark'},
                        'permissions': {'financeScope': 'self', 'manageSchedule': False, 'audit': False},
                        'today': '2026-10-08', 'timezone': 'Europe/Moscow'}
            elif endpoint == '/academy':
                data = {'lessons': photo_lessons, 'completed': list(completed), 'practices': [], 'reviews': [],
                        'totalPoints': 300, 'acceptedCount': 2, 'currentBlock': 1,
                        'blocks': [{'number': b.number, 'title': b.title, 'practiceTitle': b.practice_title,
                                    'lessonsDone': 0, 'lessonsTotal': 4, 'practiceDone': False, 'locked': False} for b in ACADEMY_BLOCKS]}
            elif endpoint == '/academy/coach':
                data = {'track': 'booking' if booking else 'photographer', 'configured': True,
                        'lessons': list(MANAGER_LESSONS) if booking else photo_lessons,
                        'completed': list(completed), 'passedQuizzes': [], 'scenarios': BOOKING_SCENARIOS if booking else [],
                        'skills': [{'key': 'light', 'label': 'Свет', 'score': 45, 'delta': 15, 'samples': 2}],
                        'diagnostic': False, 'stale': True, 'plan': None, 'canViewTeam': False,
                        'comparison': [{'id': 1, 'assignmentId': 1, 'score': 65, 'photos': [{'index': 1, 'url': '/academy/coach/reviews/1/photos/1'}]},
                                       {'id': 2, 'assignmentId': 1, 'score': 90, 'photos': [{'index': 1, 'url': '/academy/coach/reviews/2/photos/1'}]}] if not booking else []}
            elif endpoint == '/academy/coach/plan':
                plan_calls.append('booking' if booking else 'photographer')
                data = {'plan': {'id': len(plan_calls), 'title': 'AI: отработка возражений' if booking else 'AI: улучшить свет',
                                 'focus': 'objections' if booking else 'light', 'reason': 'По предыдущей AI-проверке',
                                 'lessonSlugs': ['booking-time' if booking else 'light'], 'category': '' if booking else 'woman',
                                 'scenario': 'booking-time' if booking else '', 'instructions': ['Первый шаг', 'Второй шаг', 'Третий шаг'],
                                 'successCriteria': ['Понятные условия', 'Проверяемый результат']}, 'diagnostic': False}
            elif endpoint.startswith('/academy/coach/quizzes/'):
                slug = endpoint.rsplit('/', 1)[1]
                data = grade_quiz(slug, body['answers']) if route.request.method == 'POST' else {'questions': public_quiz(slug)}
            elif endpoint.startswith('/academy/lessons/'):
                completed.add(endpoint.rsplit('/', 1)[1]); data = {'ok': True}
            elif endpoint == '/academy/development':
                data = {'configured': True, 'reviews': [], 'sessions': [session] if session else []}
            elif endpoint == '/academy/sales-training':
                session = {'id': 10, 'clientType': body['clientType'], 'status': 'ACTIVE', 'revision': 1,
                           'transcript': [{'role': 'client', 'text': 'У нас нет времени'}], 'evaluation': None}
                data = {'id': 10}
            elif endpoint == '/academy/sales-training/10/turn':
                session['transcript'].append({'role': 'employee', 'text': body['text']})
                session['revision'] += 1
                if body['finish']:
                    session['status'] = 'COMPLETED'
                    session['evaluation'] = {'score': 80, 'errors': ['Уточните время'], 'recommendations': ['Предложите два доступных слота']}
                else:
                    session['transcript'].append({'role': 'client', 'text': 'А завтра можно?'})
                data = {'ok': True}
            elif endpoint == '/academy/coach/shoots':
                data = {'id': 9, 'status': 'PENDING'} if route.request.method == 'POST' else {'shoots': [{'id': 1, 'bookingId': 1, 'photographer': 'Учебный фотограф', 'date': '2026-10-08', 'total': 5}]}
            elif endpoint.startswith('/academy/coach/reviews/'):
                return await route.fulfill(body=(ROOT / 'app/assets/training/woman/01.jpg').read_bytes(), content_type='image/jpeg')
            return await route.fulfill(body=json.dumps(data), content_type='application/json')

        await context.route('**/*', intercept)
        await page.goto(BASE + '/app/#academy')
        await page.locator('[data-academy-tab="coach"]').first.click()
        await expect(page.get_by_role('heading', name='AI: улучшить свет')).to_be_visible()
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        await page.locator('[data-coach-compare]').click()
        await expect(page.get_by_role('heading', name='До и после AI-проверки')).to_be_visible()
        assert await page.locator('img[data-coach-photo]').count() == 2
        await page.locator('[data-coach-compare-close]').click()
        await page.locator('[data-coach-shoots]').click()
        await page.locator('[data-coach-shoot="1"]').click()
        await expect(page.locator('#academyCoachStatus')).to_contain_text('поставлен в очередь')
        await page.locator('[data-coach-track="booking"]').click()
        await expect(page.get_by_role('heading', name='Академия менеджера по записи')).to_be_visible()
        await expect(page.get_by_role('heading', name='AI: отработка возражений')).to_be_visible()
        await page.locator('[data-coach-lesson="booking-approach"]').click()
        quiz = quiz_for('booking-approach')
        for i, question in enumerate(quiz):
            await page.locator(f'#academyLessonQuiz input[name="q{i}"][value="{question["correct"]}"]').check()
        await page.locator('#academyLessonQuiz button[type="submit"]').click()
        await expect(page.locator('[data-quiz-result]')).to_contain_text('Урок сохранён')
        await page.locator('[data-coach-close]').click()
        await page.locator('[data-coach-scenario="booking-time"]').first.click()
        await expect(page.locator('#bookingTrainingForm')).to_be_visible()
        await page.locator('#bookingTrainingForm textarea').fill('Понимаю. Хотите подобрать удобное время?')
        await page.locator('#bookingTrainingForm button[value="reply"]').click()
        await expect(page.locator('#bookingTraining')).to_contain_text('А завтра можно?')
        await page.locator('#bookingTrainingForm button[value="finish"]').click()
        await expect(page.get_by_role('heading', name='Результат: 80/100')).to_be_visible()
        assert completed == {'booking-approach'}
        assert 'booking' in plan_calls and 'photographer' in plan_calls
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        assert not errors, errors
        print('PASS: mobile AI plans, archived-photo comparison, real-shoot queue, manager quizzes and AI guest dialogue; no live messages')
        await browser.close()


asyncio.run(main())
