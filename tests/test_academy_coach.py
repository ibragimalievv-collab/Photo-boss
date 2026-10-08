import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import test_miniapp_release as baseline
from sqlalchemy import text

from app.academy_coach import AcademyCoach, plan_schema, skill_map
from app.miniapp_security import AccessError
from app.services.academy_curriculum import (
    BOOKING_SCENARIOS,
    MANAGER_LESSONS,
    grade_quiz,
    public_quiz,
    quiz_for,
)
from app.services.academy_growth import personal_tip
from app.services.development_ai import booking_roleplay, valid_value


class Request(dict):
    def __init__(self, uid=3, body=None, track='photographer', slug=None, roles=None):
        super().__init__(miniapp_actor={'id': uid, 'roles': roles or ['PHOTOGRAPHER']})
        self.query = {'track': track}
        self.match_info = {'slug': slug} if slug else {}
        self.payload = body if body is not None else {}
        raw = json.dumps(self.payload).encode()
        self.content, self.content_length = baseline.Body(raw), len(raw)
        self.method = "POST"

    async def json(self):
        return self.payload


def photo_result(light=6, focus=16):
    return {'status': 'completed', 'review': {'score': 75,
            'criteria': {'focus': focus, 'light': light, 'composition': 12,
                         'pose': 16, 'emotion': 12, 'variety': 12}}}


def test_weighted_skills_and_nested_tips_identify_real_weakness():
    results = [photo_result()['review'], photo_result(light=12)['review']]
    skills = {s['key']: s for s in skill_map(results, 'photographer')}
    assert skills['light']['score'] == 60 and skills['light']['delta'] == 40
    assert skills['focus']['score'] == 80
    tip = personal_tip([SimpleNamespace(ai_analysis=json.dumps(photo_result(light=12, focus=10)))])
    assert tip['criterion'] == 'focus'  # 10/20 is weaker than 12/15.
    assert all(s['score'] is None for s in skill_map([{'criteria': {'light': True}}], 'photographer'))


def test_quizzes_hide_answers_grade_all_answers_and_reject_malformed_input():
    for lesson in MANAGER_LESSONS:
        quiz = public_quiz(lesson['slug'])
        assert len(quiz) == 3 and all(set(q) == {'prompt', 'options'} for q in quiz)
        answers = [q['correct'] for q in quiz_for(lesson['slug'])]
        assert grade_quiz(lesson['slug'], answers)['passed']
        assert not grade_quiz(lesson['slug'], [(a + 1) % 3 for a in answers])['passed']
    for answers in ([], [True, 1, 2], ['0', 1, 2], [99, 1, 2]):
        with pytest.raises(ValueError):
            grade_quiz('booking-approach', answers)


def test_plan_schema_rejects_unknown_lessons_scenarios_and_wrong_ranges():
    schema = plan_schema('booking', list(MANAGER_LESSONS), [])
    plan = {'title': 'Разговор', 'focus': 'objections', 'reason': 'По последнему диалогу',
            'lessonSlugs': ['booking-time'], 'category': '', 'scenario': 'booking-time',
            'instructions': ['Спросить', 'Выслушать', 'Предложить'], 'successCriteria': ['Нет давления', 'Условия объяснены']}
    assert valid_value(plan, schema)
    assert not valid_value(plan | {'focus': 'made-up'}, schema)
    assert not valid_value(plan | {'lessonSlugs': ['private-lesson']}, schema)
    assert not valid_value(plan | {'scenario': 'discount'}, schema)
    assert not valid_value(True, {'type': 'integer', 'minimum': 0, 'maximum': 100})
    assert not valid_value(101, {'type': 'integer', 'minimum': 0, 'maximum': 100})


def test_large_original_is_reduced_for_ai_without_changing_saved_bytes():
    import io
    import random

    from PIL import Image

    from app.services.development_ai import coaching_image
    image = Image.frombytes('RGB', (2500, 2500), random.Random(7).randbytes(2500*2500*3))
    stream = io.BytesIO()
    image.save(stream, format='JPEG', quality=100)
    raw = stream.getvalue()
    assert 8*1024*1024 < len(raw) < 20*1024*1024
    prepared = coaching_image(raw)
    assert len(prepared) < 8*1024*1024 and raw == stream.getvalue()
    with Image.open(io.BytesIO(prepared)) as result:
        assert max(result.size) <= 2048
    with pytest.raises(ValueError):
        coaching_image(b'not-an-image')


class CoachingTests(unittest.IsolatedAsyncioTestCase):
    call = baseline.MiniAppTests.call
    asyncTearDown = baseline.MiniAppTests.asyncTearDown
    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        self.coach = AcademyCoach(self.service)

    async def test_old_accepted_practice_survives_more_than_thirty_attempts(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO academy_lesson_progress(user_id,topic_slug) VALUES (3,'light')"))
            for i in range(1, 37):
                conn.execute(text('INSERT INTO training_assignments(id,user_id,category_slug,status) VALUES (:id,3,:cat,:status)'),
                             {'id': i, 'cat': 'woman' if i == 1 else 'man', 'status': 'COMPLETED' if i == 1 else 'ACTIVE'})
        _, data, _ = await self.call('/academy', uid=1003)
        assert data['blocks'][0]['practiceDone'] and not data['lessons'][1]['locked']
        assert data['totalPoints'] == 110 and data['acceptedCount'] == 1
        assert json.loads((await self.coach.listing(Request())).text)['practiceReady'] is True

    async def test_personal_results_are_isolated_and_reshoot_images_are_archived(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO training_assignments(id,user_id,category_slug,status,ai_analysis) VALUES(1,3,'woman','COMPLETED',:data)"), {'data': json.dumps(photo_result())})
            for i in (1, 2):
                conn.execute(text('INSERT INTO academy_practice_reviews(id,assignment_id,result,photos,created_at) VALUES (:id,1,:data,:photos,CURRENT_TIMESTAMP)'),
                             {'id': i, 'data': json.dumps(photo_result(light=6 * i)), 'photos': json.dumps([{'index': 1, 'file': f'saved-original-{i}'}])})
        own = json.loads((await self.coach.listing(Request())).text)
        other = json.loads((await self.coach.listing(Request(uid=5))).text)
        assert own['skills'][1]['score'] == 60 and len(own['comparison']) == 2
        assert own['practiceReady'] is False
        assert all(s['score'] is None for s in other['skills']) and not other['comparison']
        req = Request(uid=5); req.match_info = {'id': '1', 'index': '1'}
        with pytest.raises(AccessError) as error:
            await self.coach.photo(req)
        assert error.value.status == 404

    async def test_ai_plan_is_persisted_idempotent_and_updates_after_new_evidence(self):
        schema = plan_schema('photographer', [self.service.lessons[0]], ['woman'])
        plan = {'title': 'Диагностическая серия', 'focus': 'light', 'reason': 'Сначала проверим свет',
                'lessonSlugs': ['light'], 'category': 'woman', 'scenario': '',
                'instructions': ['Выберите свет', 'Снимите пять разных кадров', 'Загрузите набор'],
                'successCriteria': ['Пять разных ракурсов', 'Лица в резкости']}
        assert valid_value(plan, schema)
        provider = AsyncMock(return_value={'status': 'completed', 'data': plan})
        with patch('app.academy_coach.config', SimpleNamespace(openai_api_key='fixture')), patch('app.academy_coach.structured', provider):
            first = json.loads((await self.coach.generate(Request(body={}))).text)
            second = json.loads((await self.coach.generate(Request(body={}))).text)
            assert first['plan']['id'] == second['plan']['id'] and provider.await_count == 1
            assert first['plan']['diagnostic']
            with self.engine.inner.begin() as conn:
                conn.execute(text("INSERT INTO training_assignments(user_id,category_slug,status,ai_analysis) VALUES(3,'woman','ACTIVE',:data)"), {'data': json.dumps(photo_result())})
            third = json.loads((await self.coach.generate(Request(body={}))).text)
            assert third['plan']['id'] != first['plan']['id'] and not third['plan']['diagnostic']
            provider.return_value = {'status': 'unavailable'}
            with self.engine.inner.begin() as conn:
                conn.execute(text("INSERT INTO academy_lesson_progress(user_id,topic_slug) VALUES(3,'light')"))
            with pytest.raises(AccessError) as error:
                await self.coach.generate(Request(body={}))
            assert error.value.status == 503
            saved = json.loads((await self.coach.listing(Request())).text)
            assert saved['plan']['id'] == third['plan']['id'] and saved['stale']

    async def test_manager_lesson_requires_own_passed_quiz_and_awards_progress_once(self):
        slug = MANAGER_LESSONS[0]['slug']
        with pytest.raises(AccessError) as error:
            await self.service.complete_lesson(Request(uid=4, slug=slug, roles=['MANAGER']))
        assert error.value.status == 409
        answers = [q['correct'] for q in quiz_for(slug)]
        await self.coach.quiz(Request(uid=4, slug=slug, track='booking', body={'answers': answers}))
        await self.service.complete_lesson(Request(uid=4, slug=slug, roles=['MANAGER']))
        await self.service.complete_lesson(Request(uid=4, slug=slug, roles=['MANAGER']))
        data = json.loads((await self.coach.listing(Request(uid=4, track='booking'))).text)
        assert data['completed'] == [slug] and len(data['lessons']) == 8
        with pytest.raises(AccessError):
            await self.service.complete_lesson(Request(uid=3, slug=slug))

    async def test_learning_team_view_is_admin_only_and_excludes_financial_data(self):
        with pytest.raises(AccessError) as error:
            await self.coach.team(Request())
        assert error.value.status == 403
        data = json.loads((await self.coach.team(Request(uid=2, roles=['ADMIN']))).text)
        assert {r['track'] for r in data['items']} == {'photographer', 'booking'}
        assert all(not {'sales', 'amount', 'payroll'} & set(r) for r in data['items'])

    async def test_real_shoot_queue_is_private_and_duplicate_requests_reuse_review(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text('INSERT INTO shootings(id,booking_id) VALUES(1,1)'))
            conn.execute(text('INSERT INTO photos(id,shooting_id) VALUES(1,1),(2,1)'))
        with pytest.raises(AccessError) as error:
            await self.coach.request_shoot(Request(uid=5, body={'bookingId': 1}))
        assert error.value.status == 404
        first = json.loads((await self.coach.request_shoot(Request(body={'bookingId': 1}))).text)
        second = json.loads((await self.coach.request_shoot(Request(body={'bookingId': 1}))).text)
        assert first['id'] == second['id']
        assert len(json.loads((await self.coach.shoots(Request(uid=5))).text)['shoots']) == 0
        admin = json.loads((await self.coach.shoots(Request(uid=2, roles=['ADMIN']))).text)
        assert len(admin['shoots']) == 1


def test_booking_ai_validates_six_skills_and_does_not_fabricate_provider_errors():
    import asyncio

    async def run():
        data = {'reply': 'Спокойнее объясните условия', 'score': 80,
                'errors': ['Не уточнили время'], 'recommendations': ['Предложите два доступных слота'],
                'criteria': {key: 80 for key in ('approach', 'contact', 'conditions', 'objections', 'booking', 'respect')}}
        with patch('app.services.development_ai.structured', AsyncMock(return_value={'status': 'completed', 'data': data})) as provider:
            assert (await booking_roleplay(BOOKING_SCENARIOS[0]['type'], [{'role': 'employee', 'text': 'Можно рассказать?'}], True))['status'] == 'completed'
            assert 'не продаёт готовые фотографии' in provider.call_args.args[0]
            provider.return_value = {'status': 'completed', 'data': data | {'score': 99}}
            assert (await booking_roleplay(BOOKING_SCENARIOS[0]['type'], [], True))['status'] == 'unavailable'
            provider.return_value = {'status': 'unavailable'}
            assert (await booking_roleplay(BOOKING_SCENARIOS[0]['type'], [], True))['status'] == 'unavailable'
    asyncio.run(run())
