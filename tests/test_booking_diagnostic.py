import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_insights import fixture

from app.academy_diagnostic import NAMESPACE, AcademyDiagnostic, load_diagnostic
from app.miniapp_security import AccessError
from app.models import AcademyLessonProgress, OperationRequest
from app.services.academy_curriculum import MANAGER_LESSONS
from app.services.booking_diagnostic import QUESTIONS, SKILLS, assess, public_questions


def answers_for(points):
    return [question['scores'].index(points) for question in QUESTIONS]


def request(uid=1, roles=None, body=None):
    return {'miniapp_actor': {'id': uid, 'roles': roles or ['MANAGER']}, 'body': body}


def test_public_diagnostic_hides_keys_and_covers_all_six_skills():
    public = public_questions()
    assert len(public) == 10 and {q['skill'] for q in QUESTIONS} == set(SKILLS)
    assert all(set(q) == {'id', 'prompt', 'options'} and len(q['options']) == 3 for q in public)
    public[0]['options'][0] = 'changed'
    assert public_questions()[0]['options'][0] != 'changed'


def test_scoring_domain_weaknesses_and_provisional_level_are_server_defined():
    high = assess(answers_for(100))
    assert high['score'] == 100 and high['level'] == 'confident'
    assert high['provisional'] and high['roleplayRequired']
    assert all(skill['score'] == 100 for skill in high['skills'])
    assert assess(answers_for(50))['level'] == 'developing'
    assert assess(answers_for(0))['level'] == 'beginner'
    weak = answers_for(100)
    index = next(i for i, q in enumerate(QUESTIONS) if q['id'] == 'consent')
    weak[index] = QUESTIONS[index]['scores'].index(0)
    result = assess(weak)
    assert result['score'] == 90 and result['level'] == 'developing'
    assert result['skills'][-1]['score'] == 0
    assert result['recommendedLessonSlugs'][0] == 'booking-approach'
    assert set(result['recommendedLessonSlugs']) <= {lesson['slug'] for lesson in MANAGER_LESSONS}


@pytest.mark.parametrize('answers', [None, {}, [], [0] * 9, [0] * 11, [True] * 10,
                                     ['0'] * 10, [3] * 10, [-1] * 10, [0.0] * 10])
def test_answers_are_strict_and_bounded(answers):
    with pytest.raises(ValueError):
        assess(answers)


def test_attempts_immutable_idempotent_private_and_leave_learning_progress_unchanged():
    async def run():
        engine, factory, api = await fixture()
        api.body = AsyncMock(side_effect=lambda request, **kwargs: request['body'])
        service = AcademyDiagnostic(api)
        first = {'answers': answers_for(100), 'requestId': 'diagnostic-attempt-0001'}
        try:
            before = json.loads((await service.listing(request())).text)
            assert before['assessment'] is None and len(before['questions']) == 10
            initial = json.loads((await service.submit(request(body=first))).text)
            repeated = json.loads((await service.submit(request(body=first))).text)
            assert initial['assessment'] == repeated['assessment']
            assert not initial['replayed'] and repeated['replayed']
            with pytest.raises(AccessError) as conflict:
                await service.submit(request(body=first | {'answers': answers_for(0)}))
            assert conflict.value.status == 409
            second = first | {'answers': answers_for(0), 'requestId': 'diagnostic-attempt-0002'}
            latest = json.loads((await service.submit(request(body=second))).text)['assessment']
            assert latest['level'] == 'beginner'
            async with engine.connect() as conn:
                assert (await load_diagnostic(api, conn, 1)) == latest
                assert await load_diagnostic(api, conn, 2) is None
            assert json.loads((await service.listing(request(uid=2))).text)['assessment'] is None
            # The same client key belongs to a different user's independent attempt.
            await service.submit(request(uid=2, body=first))
            async with factory() as session:
                rows = (await session.scalars(select(OperationRequest).where(
                    OperationRequest.request_key.like(NAMESPACE + '%')))).all()
                assert len(rows) == 3
                assert json.loads(rows[0].result)['assessment'] == initial['assessment']
                assert not (await session.scalars(select(AcademyLessonProgress))).all()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_roles_body_shape_and_request_ids_are_checked_before_persistence():
    async def run():
        engine, factory, api = await fixture()
        api.body = AsyncMock(side_effect=lambda request, **kwargs: request['body'])
        service = AcademyDiagnostic(api)
        good = {'answers': answers_for(100), 'requestId': 'diagnostic-attempt-0001'}
        try:
            for role in ('GUEST', 'UNKNOWN'):
                for method in (service.listing, service.submit):
                    with pytest.raises(AccessError) as denied:
                        await method(request(roles=[role], body=good))
                    assert denied.value.status == 403
            for body in (None, [], good | {'score': 100}, good | {'requestId': 'short'},
                         good | {'requestId': 'x' * 49}, good | {'requestId': 'wrong request id'},
                         good | {'requestId': 123456789012}, good | {'answers': [True] * 10}):
                with pytest.raises(AccessError) as invalid:
                    await service.submit(request(body=body))
                assert invalid.value.status == 400
            async with factory() as session:
                assert not (await session.scalars(select(OperationRequest))).all()
            for role in ('MANAGER', 'OWNER', 'ADMIN', 'PHOTOGRAPHER'):
                assert json.loads((await service.listing(request(roles=[role]))).text)['assessment'] is None
        finally:
            await engine.dispose()
    asyncio.run(run())
