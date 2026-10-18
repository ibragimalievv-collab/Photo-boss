import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import test_miniapp_release as baseline
from sqlalchemy import text

from app.academy_coach import AcademyCoach
from app.academy_mentor import (
    ANSWER_SCHEMA,
    AcademyMentor,
    lesson_context,
    voice_evidence,
)
from app.miniapp_security import AccessError
from app.services.development_ai import valid_value


class Request(dict):
    def __init__(self, uid=3, body=None, roles=None, track='photographer'):
        super().__init__(miniapp_actor={'id': uid, 'roles': roles or ['PHOTOGRAPHER']})
        self.query = {'track': track}
        self.payload = {} if body is None else body
        raw = json.dumps(self.payload).encode()
        self.content, self.content_length = baseline.Body(raw), len(raw)


def reply(source='lesson:light'):
    return {'answer': 'Проверьте выдержку и точку автофокуса. Причину по старому описанию установить нельзя.',
            'observations': [], 'steps': ['Снимите неподвижный объект и сравните фокус.'],
            'exercise': 'Серия с разными выдержками.', 'limitations': ['Оригиналы сейчас не анализируются.'],
            'sourceIds': [source]}


def review():
    return {'status': 'completed', 'review': {'score': 72,
            'criteria': {'focus': 16, 'light': 6, 'composition': 12, 'pose': 14, 'emotion': 12, 'variety': 12},
            'issues': ['В кадре 2 тень на лице.'], 'next_action': 'Поверните лицо к открытому небу.',
            'frames': [{'index': 2, 'observation': 'Тень на лице', 'action': 'Повернуть к свету',
                        'uncertainty': 'EXIF неизвестен', 'confidence': 'MEDIUM'}]}}


class MentorTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = baseline.MiniAppTests.asyncTearDown

    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        with self.engine.inner.begin() as conn:
            conn.execute(text('''CREATE TABLE IF NOT EXISTS operation_requests(id INTEGER PRIMARY KEY,user_id INTEGER,
                request_key TEXT,payload_hash TEXT,result TEXT,created_at DATETIME,UNIQUE(user_id,request_key))'''))
        self.mentor = AcademyMentor(self.service, AcademyCoach(self.service))
        self.config = patch('app.academy_mentor.config', SimpleNamespace(openai_api_key='server-secret'))
        self.config.start()
        self.addCleanup(self.config.stop)

    def seed(self):
        with self.engine.inner.begin() as conn:
            conn.execute(text("INSERT INTO training_assignments(id,user_id,category_slug,status,ai_analysis) VALUES(1,3,'woman','COMPLETED',:value)"), {'value': json.dumps(review())})
            conn.execute(text('''INSERT INTO academy_practice_reviews(id,assignment_id,result,photos,created_at)
                VALUES(7,1,:value,'[]',CURRENT_TIMESTAMP)'''), {'value': json.dumps(review())})

    async def test_saved_snapshot_is_fast_no_provider_and_strictly_own_user(self):
        self.seed()
        with patch('app.academy_mentor.structured', AsyncMock()) as provider:
            own = json.loads((await self.mentor.listing(Request())).text)
            other = json.loads((await self.mentor.listing(Request(uid=5))).text)
        provider.assert_not_awaited()
        assert own['sources'][0]['id'] == 'practice:7'
        assert own['sources'][0]['summary']['frames'][0]['action'] == 'Повернуть к свету'
        assert own['skills'][1]['score'] == 40
        assert not other['sources'] and all(skill['score'] is None for skill in other['skills'])
        assert 'server-secret' not in json.dumps(own)
        assert '_lessons' not in own and '_fingerprint' not in own

    async def test_ask_replays_and_caches_without_reanalysing_images(self):
        self.seed()
        provider = AsyncMock(return_value={'status': 'completed', 'data': reply('practice:7')})
        body = {'question': 'Как улучшить свет?', 'requestId': 'request_1234567890'}
        with patch('app.academy_mentor.structured', provider):
            first = json.loads((await self.mentor.ask(Request(body=body))).text)
            second = json.loads((await self.mentor.ask(Request(body=body))).text)
            third = json.loads((await self.mentor.ask(Request(body=body | {'requestId': 'request_1234567891'}))).text)
        assert not first['cached'] and second['replayed'] and third['cached']
        assert provider.await_count == 1
        assert first['sources'][0]['id'] == 'practice:7' and first['basedOnSavedReviews']
        content = json.loads(provider.await_args.args[1][0]['text'])
        assert content['savedReviews'][0]['id'] == 'practice:7'
        assert all(part['type'] == 'input_text' for part in provider.await_args.args[1])
        assert 'EXIF' in provider.await_args.args[0] and 'server-secret' not in str(provider.await_args)
        with pytest.raises(AccessError) as mismatch:
            await self.mentor.ask(Request(body=body | {'question': 'Другой вопрос'}))
        assert mismatch.value.status == 409

    async def test_followup_keeps_own_track_conversation_and_not_another_users_chat(self):
        provider = AsyncMock(return_value={'status': 'completed', 'data': reply()})
        with patch('app.academy_mentor.structured', provider):
            await self.mentor.ask(Request(body={'question': 'Как проверить фокус?', 'requestId': 'chat_first_123456'}))
            await self.mentor.ask(Request(body={'question': 'Объясни подробнее', 'requestId': 'chat_next_1234567'}))
            content = json.loads(provider.await_args.args[1][0]['text'])
            assert content['conversation'][0]['question'] == 'Как проверить фокус?'
            assert content['conversation'][0]['answer'] == reply()['answer']
            await self.mentor.ask(Request(uid=5, body={'question': 'Объясни подробнее', 'requestId': 'chat_other_123456'}))
            other = json.loads(provider.await_args.args[1][0]['text'])
            assert other['conversation'] == []

    async def test_new_evidence_invalidates_question_cache_but_not_original_receipt(self):
        provider = AsyncMock(return_value={'status': 'completed', 'data': reply()})
        body = {'question': 'Как улучшить свет?', 'requestId': 'request_1234567890'}
        with patch('app.academy_mentor.structured', provider):
            await self.mentor.ask(Request(body=body))
            self.seed()
            original = json.loads((await self.mentor.ask(Request(body=body))).text)
            await self.mentor.ask(Request(body=body | {'requestId': 'request_1234567891'}))
        assert original['replayed'] and provider.await_count == 2

    async def test_provider_failure_is_not_fake_answer_and_same_request_can_retry(self):
        body = {'question': 'Что такое диафрагма?', 'requestId': 'request_1234567890'}
        provider = AsyncMock(return_value={'status': 'unavailable', 'message': 'Провайдер недоступен'})
        with patch('app.academy_mentor.structured', provider):
            with pytest.raises(AccessError) as failed:
                await self.mentor.ask(Request(body=body))
            assert failed.value.status == 503
            saved = json.loads((await self.mentor.listing(Request())).text)
            assert not saved['history']
            provider.return_value = {'status': 'completed', 'data': reply()}
            retried = json.loads((await self.mentor.ask(Request(body=body))).text)
            assert retried['answer']['answer']

    async def test_made_up_source_ids_are_rejected(self):
        provider = AsyncMock(return_value={'status': 'completed', 'data': reply('practice:other-user')})
        with patch('app.academy_mentor.structured', provider), pytest.raises(AccessError) as rejected:
            await self.mentor.ask(Request(body={'question': 'Почему нерезко?', 'requestId': 'request_1234567890'}))
        assert rejected.value.status == 503

    async def test_staff_validation_body_limit_and_rate_limit(self):
        with pytest.raises(AccessError) as denied:
            await self.mentor.listing(Request(roles=['GUEST']))
        assert denied.value.status == 403
        for body in ({'question': 'a', 'requestId': 'validrequest123'}, {'question': 'Свет', 'requestId': 'bad'},
                     {'question': 'Свет', 'requestId': 'validrequest123', 'userId': 5}):
            with pytest.raises(AccessError) as bad:
                await self.mentor.ask(Request(body=body))
            assert bad.value.status == 400
        provider = AsyncMock(return_value={'status': 'completed', 'data': reply()})
        with patch('app.academy_mentor.structured', provider):
            for i in range(6):
                await self.mentor.ask(Request(body={'question': f'Как улучшить свет {i}?', 'requestId': f'request_123456789{i}'}))
            with pytest.raises(AccessError) as rate:
                await self.mentor.ask(Request(body={'question': 'Седьмой вопрос?', 'requestId': 'request_1234567899'}))
        assert rate.value.status == 429 and provider.await_count == 6

    async def test_same_question_during_provider_call_has_one_paid_reservation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def answer(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {'status': 'completed', 'data': reply()}
        body = {'question': 'Как улучшить свет?', 'requestId': 'request_1234567890'}
        with patch('app.academy_mentor.structured', AsyncMock(side_effect=answer)) as provider:
            first = asyncio.create_task(self.mentor.ask(Request(body=body)))
            await entered.wait()
            with pytest.raises(AccessError) as pending:
                await self.mentor.ask(Request(body=body | {'requestId': 'request_1234567891'}))
            assert pending.value.status == 409
            release.set()
            await first
        assert provider.await_count == 1


def test_lesson_retrieval_is_relevant_and_bounded_and_voice_has_personal_evidence():
    lessons = [{'slug': 'light', 'title': 'Свет', 'body': 'Свет ' * 20000},
               {'slug': 'focus', 'title': 'Фокус', 'body': 'Фокус и резкость'}]
    selected = lesson_context(lessons, 'Как настроить фокус?')
    assert selected[0]['id'] == 'lesson:focus'
    assert sum(len(item['body']) for item in selected) <= 18000
    snapshot = {'track': 'photographer', 'skills': [{'key': 'light', 'score': 40}],
                'plan': {'title': 'Освоить свет', 'instructions': ['Снять в тени']},
                'sources': [{'id': 'practice:7', 'label': 'Практика №7', 'summary': review()['review']}]}
    voice = voice_evidence(snapshot)
    assert voice['reviews'][0]['id'] == 'practice:7' and voice['skills'][0]['score'] == 40
    assert len(json.dumps(voice, ensure_ascii=False)) <= 14000
    assert valid_value(reply(), ANSWER_SCHEMA)
