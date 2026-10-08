"""AI-only learning plans, normalized skills, quizzes and real-shoot requests."""
import asyncio
import hashlib
import json

from aiohttp import web
from sqlalchemy import text

from .config import config
from .miniapp_security import AccessError
from .models import utc_now
from .people import positive_id
from .services.academy_curriculum import (
    BOOKING_RULES,
    BOOKING_SCENARIOS,
    MANAGER_LESSONS,
    grade_quiz,
    public_quiz,
)
from .services.development_ai import STR, STRINGS, obj, structured, valid_value
from .services.insights import parsed
from .services.training import CATEGORY_BY_SLUG

PHOTO_SKILLS = {'focus': ('Фокус и техника', 20), 'light': ('Свет', 15),
                'composition': ('Композиция', 15), 'pose': ('Позирование', 20),
                'emotion': ('Выразительность кадра', 15), 'variety': ('Разнообразие', 15)}
BOOKING_SKILLS = {'approach': ('Начало разговора', 100), 'contact': ('Контакт и вопросы', 100),
                  'conditions': ('Объяснение условий', 100), 'objections': ('Возражения', 100),
                  'booking': ('Договорённость о записи', 100), 'respect': ('Уважение к гостю', 100)}


def review_data(value):
    data = parsed(value) if isinstance(value, str) else value
    if not isinstance(data, dict):
        return {}
    if data.get('status') == 'completed':
        return data.get('review', {})
    return data if 'criteria' in data else {}


def skill_map(reviews, track):
    """Percentages compare different rubric weights; oldest-to-newest evidence."""
    limits = PHOTO_SKILLS if track == 'photographer' else BOOKING_SKILLS
    result = []
    for key, (label, maximum) in limits.items():
        values = [round(r['criteria'][key] / maximum * 100) for r in reviews
                  if isinstance(r.get('criteria'), dict) and type(r['criteria'].get(key)) is int
                  and 0 <= r['criteria'][key] <= maximum]
        result.append({'key': key, 'label': label,
                       'score': round(sum(values[-5:]) / len(values[-5:])) if values else None,
                       'delta': values[-1] - values[0] if len(values) > 1 else None,
                       'samples': len(values)})
    return result


def plan_schema(track, lessons, categories):
    return obj({'title': {**STR, 'minLength': 1, 'maxLength': 160},
                'focus': {'type': 'string', 'enum': list(PHOTO_SKILLS if track == 'photographer' else BOOKING_SKILLS)},
                'reason': {**STR, 'minLength': 1},
                'lessonSlugs': {'type': 'array', 'items': {'type': 'string', 'enum': [x['slug'] for x in lessons]}, 'minItems': 1, 'maxItems': 4},
                'category': {'type': 'string', 'enum': categories or ['']},
                'scenario': {'type': 'string', 'enum': [x['type'] for x in BOOKING_SCENARIOS] if track == 'booking' else ['']},
                'instructions': {**STRINGS, 'minItems': 3, 'maxItems': 5},
                'successCriteria': {**STRINGS, 'minItems': 2, 'maxItems': 4}})


class AcademyCoach:
    def __init__(self, api):
        self.api, self.engine = api, api.engine
        self.locks = {}

    def track(self, request):
        track = request.query.get('track', 'photographer')
        if track not in ('photographer', 'booking'):
            raise AccessError('Выберите направление обучения.', 400)
        return track

    async def evidence(self, uid, track):
        async with self.engine.connect() as conn:
            lessons = self.api.lessons if track == 'photographer' else list(MANAGER_LESSONS)
            progress = await self.api.rows(conn, 'SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid', uid=uid)
            quizzes = await self.api.rows(conn, 'SELECT DISTINCT topic_slug FROM academy_quiz_attempts WHERE user_id=:uid AND passed=TRUE', uid=uid)
            shoot = []
            comparison = []
            allowed = []
            practice_ready = True
            if track == 'photographer':
                snapshots = await self.api.rows(conn, '''SELECT r.id,r.assignment_id,r.result,r.photos,r.created_at
                    FROM academy_practice_reviews r JOIN training_assignments a ON a.id=r.assignment_id
                    WHERE a.user_id=:uid ORDER BY r.id DESC LIMIT 50''', uid=uid)
                assignments = await self.api.rows(conn, '''SELECT id,category_slug,status,ai_analysis FROM training_assignments
                    WHERE user_id=:uid ORDER BY id''', uid=uid)
                snapshot_assignments = {r['assignment_id'] for r in snapshots}
                reviews = [review_data(r['ai_analysis']) for r in assignments if r['id'] not in snapshot_assignments]
                reviews += [review_data(r['result']) for r in reversed(snapshots)]
                # Keep original frames even when a reshoot replaces a submission.
                if len(snapshots) >= 2:
                    same = [r for r in reversed(snapshots) if r['assignment_id'] == snapshots[0]['assignment_id']]
                    chosen = [same[0], same[-1]] if len(same) >= 2 else [snapshots[-1], snapshots[0]]
                    comparison = [{'id': r['id'], 'assignmentId': r['assignment_id'],
                                   'score': review_data(r['result']).get('score'), 'at': str(r['created_at']),
                                   'photos': [{'index': p['index'], 'url': f"/academy/coach/reviews/{r['id']}/photos/{p['index']}"}
                                              for p in json.loads(r['photos'])]} for r in chosen]
                shoot = await self.api.rows(conn, "SELECT id,result,analyzed,created_at FROM shoot_development_reviews WHERE photographer_id=:uid AND status='COMPLETED' ORDER BY id DESC LIMIT 5", uid=uid)
                completed = {r['topic_slug'] for r in progress}
                accepted = {r['category_slug'] for r in assignments if r['status'] == 'COMPLETED'}
                block_number = self.api.academy_state(completed, accepted)
                block = next((b for b in self.api.blocks if b['number'] == block_number), None)
                practice_ready = all(x["slug"] in completed for x in lessons if x["block"] == block_number) or any(a["status"] in {"ACTIVE", "PENDING_REVIEW"} for a in assignments)
                course_done = all(x['slug'] in completed for x in lessons) and all(
                    not b['practiceCategories'] or bool(set(b['practiceCategories']) & accepted) for b in self.api.blocks)
                allowed = list(CATEGORY_BY_SLUG) if course_done else list(block['practiceCategories']) if block else list(CATEGORY_BY_SLUG)
                if not allowed:
                    allowed = list(CATEGORY_BY_SLUG)
                lessons = [dict(x, locked=x['block'] > block_number) for x in lessons]
            else:
                sessions = await self.api.rows(conn, "SELECT id,evaluation FROM sales_training_sessions WHERE user_id=:uid AND client_type LIKE 'booking-%' AND status='COMPLETED' ORDER BY id DESC LIMIT 50", uid=uid)
                reviews = [parsed(r['evaluation']) for r in reversed(sessions)]
            skill_reviews = list(reviews)
            if track == 'photographer':
                for row in reversed(shoot):
                    frames = [f for batch in json.loads(row['analyzed'] or '[]') for f in batch.get('frames', [])]
                    criteria = {}
                    for key, (_, maximum) in PHOTO_SKILLS.items():
                        values = [f['criteria'][key] for f in frames if isinstance(f.get('criteria'), dict)
                                  and type(f['criteria'].get(key)) is int and 0 <= f['criteria'][key] <= maximum]
                        if values:
                            criteria[key] = round(sum(values) / len(values))
                    if criteria:
                        skill_reviews.append({'criteria': criteria})
            skills = skill_map(skill_reviews, track)
            done = sorted({r['topic_slug'] for r in progress} & {x['slug'] for x in lessons})
            evidence = {'track': track, 'skills': skills, 'reviews': reviews[-10:],
                        'shoots': [{'id': r['id'], 'result': parsed(r['result'])} for r in shoot], 'completed': done,
                        'allowedCategories': sorted(allowed), 'practiceReady': practice_ready, 'version': 1}
            fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            plans = await self.api.rows(conn, '''SELECT id,data,fingerprint FROM academy_coaching_plans
                WHERE user_id=:uid AND track=:track ORDER BY id DESC LIMIT 1''', uid=uid, track=track)
        return {'track': track, 'practiceReady': practice_ready, 'configured': bool(config.openai_api_key), 'skills': skills,
                'lessons': lessons, 'completed': done, 'passedQuizzes': [r['topic_slug'] for r in quizzes],
                'comparison': comparison, 'plan': ({'id': plans[0]['id'], **parsed(plans[0]['data'])} if plans else None),
                'stale': not plans or plans[0]['fingerprint'] != fingerprint,
                'diagnostic': not any(x['samples'] for x in skills) and not shoot,
                'scenarios': BOOKING_SCENARIOS if track == 'booking' else [],
                'rules': BOOKING_RULES if track == 'booking' else '',
                '_evidence': evidence, '_fingerprint': fingerprint}

    async def listing(self, request):
        data = await self.evidence(request['miniapp_actor']['id'], self.track(request))
        return web.json_response({k: v for k, v in data.items() if not k.startswith('_')} | {'canViewTeam': bool({'OWNER', 'ADMIN'} & set(request['miniapp_actor']['roles']))})

    async def generate(self, request):
        actor, track = request['miniapp_actor'], self.track(request)
        if await self.api.body(request) != {}:
            raise AccessError('Задание формируется по сохранённым результатам.', 400)
        async with self.locks.setdefault((actor['id'], track), asyncio.Lock()):
            data = await self.evidence(actor['id'], track)
            if not data['stale']:
                return web.json_response({'plan': data['plan'], 'diagnostic': data['diagnostic']})
            if not config.openai_api_key:
                raise AccessError('AI-помощник не подключён. Уроки и сохранённые результаты доступны.', 503)
            lessons = [x for x in data['lessons'] if not x.get('locked')]
            schema = plan_schema(track, lessons, data['_evidence']['allowedCategories'])
            instructions = (
                'Ты AI-наставник Photo Boss. Сам назначь одно конкретное упражнение по подтверждённым слабым местам. '
                'Сравнивай критерии в процентах: они имеют разный максимальный балл. '
                'При отсутствии оценок дай диагностическое упражнение, не называй выдуманные слабости. '
                'Используй только доступные уроки, категорию текущего блока и существующие сценарии. Если practiceReady=false, предложи сначала непройденные уроки текущего блока. '
                'Задание фотографа включает пять различных кадров по плану категории; дополнительные указания развивают слабый критерий. '
                'Задание менеджера по записи — диалог с гостем до записи, а не продажа фотографий. '
                'Уважай отказ, объясняй условия и согласие на контакт. Не обещай скидок. '
                'Дай 3–5 шагов и 2–4 проверяемых критерия успеха. Не меняй зарплаты, статусы работы и назначения съёмок. '
                + BOOKING_RULES)
            response = await structured(instructions, [{'type': 'input_text', 'text': json.dumps(
                {'evidence': data['_evidence'], 'lessons': lessons, 'scenarios': data['scenarios']}, ensure_ascii=False)}], schema, 'academy_personal_plan', limit=2400)
            if response['status'] != 'completed' or not valid_value(response.get('data'), schema):
                raise AccessError('AI временно недоступен. Прошлый план сохранён; повторите позже.', 503)
            plan = dict(response['data'], diagnostic=data['diagnostic'])
            async with self.engine.begin() as conn:
                # The unique evidence key also protects against another service instance.
                rows = await self.api.rows(conn, '''INSERT INTO academy_coaching_plans
                    (user_id,track,fingerprint,data,created_at) VALUES (:uid,:track,:fp,:data,:now)
                    ON CONFLICT (user_id,track,fingerprint) DO UPDATE SET fingerprint=excluded.fingerprint RETURNING id,data''',
                    uid=actor['id'], track=track, fp=data['_fingerprint'], data=json.dumps(plan, ensure_ascii=False), now=utc_now())
                await self.api.audit_write(conn, actor, 'academy_ai_plan', 'academy_plan', rows[0]['id'], track)
            return web.json_response({'plan': {'id': rows[0]['id'], **parsed(rows[0]['data'])}, 'diagnostic': data['diagnostic']})

    async def quiz(self, request):
        slug = request.match_info['slug']
        questions = public_quiz(slug)
        if not questions:
            raise AccessError('Тест не найден.', 404)
        if request.method == 'GET':
            return web.json_response({'questions': questions})
        body = await self.api.body(request)
        if set(body) != {'answers'}:
            raise AccessError('Ответьте на вопросы.', 400)
        try:
            result = grade_quiz(slug, body['answers'])
        except ValueError as exc:
            raise AccessError(str(exc), 400) from exc
        uid = request['miniapp_actor']['id']
        async with self.engine.begin() as conn:
            await conn.execute(text('''INSERT INTO academy_quiz_attempts
                (user_id,topic_slug,score,answers,passed,created_at) VALUES (:uid,:slug,:score,:answers,:passed,:now)'''),
                {'uid': uid, 'slug': slug, 'score': result['score'], 'answers': json.dumps(body['answers']), 'passed': result['passed'], 'now': utc_now()})
        return web.json_response(result)

    async def photo(self, request):
        rid, index = positive_id(request.match_info['id']), positive_id(request.match_info['index'])
        async with self.engine.connect() as conn:
            rows = await self.api.rows(conn, '''SELECT r.photos,a.user_id FROM academy_practice_reviews r
                JOIN training_assignments a ON a.id=r.assignment_id WHERE r.id=:id''', id=rid)
        actor = request['miniapp_actor']
        if not rows or (rows[0]['user_id'] != actor['id'] and not {'OWNER', 'ADMIN'} & set(actor['roles'])):
            raise AccessError('Фото не найдено.', 404)
        photo = next((p for p in json.loads(rows[0]['photos']) if p['index'] == index), None)
        if not photo:
            raise AccessError('Фото не найдено.', 404)
        from .academy_practice import photo_bytes
        raw = await photo_bytes(self.api.bot, request.app['yandex_disk'], photo['file'])
        return web.Response(body=raw, content_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})

    async def team(self, request):
        actor = request['miniapp_actor']
        if not {'OWNER', 'ADMIN'} & set(actor['roles']):
            raise AccessError('Прогресс команды доступен владельцу и администратору.', 403)
        async with self.engine.connect() as conn:
            staff = await self.api.rows(conn, """SELECT u.id,u.name,ur.role FROM users u JOIN user_roles ur ON ur.user_id=u.id
                WHERE u.active=TRUE AND ur.role IN ('PHOTOGRAPHER','MANAGER') ORDER BY u.name LIMIT 100""")
            plans = await self.api.rows(conn, 'SELECT user_id,track,data FROM academy_coaching_plans ORDER BY id DESC LIMIT 1000')
            progress = await self.api.rows(conn, 'SELECT user_id,topic_slug FROM academy_lesson_progress')
            practice = await self.api.rows(conn, "SELECT user_id,ai_score FROM training_assignments WHERE status='COMPLETED'")
            dialogues = await self.api.rows(conn, "SELECT user_id,evaluation FROM sales_training_sessions WHERE client_type LIKE 'booking-%' AND status='COMPLETED'")
        items = []
        for user in staff:
            track = 'booking' if user['role'] == 'MANAGER' else 'photographer'
            known = {x['slug'] for x in (MANAGER_LESSONS if track == 'booking' else self.api.lessons)}
            done = {r['topic_slug'] for r in progress if r['user_id'] == user['id']} & known
            scores = [parsed(r['evaluation']).get('score') for r in dialogues if r['user_id'] == user['id']] if track == 'booking' else [r['ai_score'] for r in practice if r['user_id'] == user['id']]
            scores = [v for v in scores if type(v) is int and 0 <= v <= 100]
            plan = next((parsed(p['data']) for p in plans if p['user_id'] == user['id'] and p['track'] == track), {})
            items.append({'name': user['name'], 'track': track, 'lessons': len(done), 'totalLessons': len(known),
                          'accepted': len(scores), 'quality': round(sum(scores)/len(scores)) if scores else None,
                          'focus': plan.get('title'), 'reason': plan.get('reason')})
        return web.json_response({'items': items})

    async def shoots(self, request):
        actor = request['miniapp_actor']
        staff = bool({'OWNER', 'ADMIN'} & set(actor['roles']))
        async with self.engine.connect() as conn:
            rows = await self.api.rows(conn, '''SELECT sh.id,sh.booking_id,b.photographer_id,b.shoot_date,u.name,
                (SELECT COUNT(*) FROM photos p WHERE p.shooting_id=sh.id) AS total
                FROM shootings sh JOIN bookings b ON b.id=sh.booking_id JOIN users u ON u.id=b.photographer_id
                ''' + ('' if staff else 'WHERE b.photographer_id=:uid ') + 'ORDER BY sh.id DESC LIMIT 50', uid=actor['id'])
        return web.json_response({'shoots': [{'id': r['id'], 'bookingId': r['booking_id'], 'photographer': r['name'],
                                             'date': str(r['shoot_date']), 'total': r['total']} for r in rows]})

    async def request_shoot(self, request):
        actor, body = request['miniapp_actor'], await self.api.body(request)
        if set(body) != {'bookingId'} or type(body['bookingId']) is not int or body['bookingId'] <= 0:
            raise AccessError('Выберите съёмку.', 400)
        async with self.engine.begin() as conn:
            rows = await self.api.rows(conn, '''SELECT sh.id,b.photographer_id FROM shootings sh
                JOIN bookings b ON b.id=sh.booking_id WHERE b.id=:id''', id=body['bookingId'])
            if not rows or (rows[0]['photographer_id'] != actor['id'] and not {'OWNER', 'ADMIN'} & set(actor['roles'])):
                raise AccessError('Съёмка не найдена.', 404)
            photos = await self.api.rows(conn, 'SELECT id FROM photos WHERE shooting_id=:id ORDER BY id', id=rows[0]['id'])
            ids = [p['id'] for p in photos]
            if not ids or len(ids) > 1000:
                raise AccessError('Для AI-разбора нужны от 1 до 1000 загруженных кадров.', 409)
            fp = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
            result = await self.api.rows(conn, '''INSERT INTO shoot_development_reviews
                (shooting_id,photographer_id,fingerprint,photo_ids,status,analyzed,summary_parts,attempts,created_at)
                VALUES (:shoot,:uid,:fp,:photos,'PENDING','[]','[]',0,:now)
                ON CONFLICT (shooting_id,fingerprint) DO UPDATE SET fingerprint=excluded.fingerprint RETURNING id,status''',
                shoot=rows[0]['id'], uid=rows[0]['photographer_id'], fp=fp, photos=json.dumps(ids), now=utc_now())
        return web.json_response({'id': result[0]['id'], 'status': result[0]['status']}, status=202)


def install_academy_coach(app, api):
    service = AcademyCoach(api)
    app['academy_coach'] = service
    for method, path, handler in (
        ('GET', '/academy/coach', service.listing), ('POST', '/academy/coach/plan', service.generate),
        ('GET', '/academy/coach/quizzes/{slug}', service.quiz), ('POST', '/academy/coach/quizzes/{slug}', service.quiz),
        ('GET', '/academy/coach/reviews/{id}/photos/{index}', service.photo),
        ('GET', '/academy/coach/team', service.team),
        ('GET', '/academy/coach/shoots', service.shoots), ('POST', '/academy/coach/shoots', service.request_shoot),
    ):
        app.router.add_route(method, '/api/miniapp' + path, handler)
    return service
