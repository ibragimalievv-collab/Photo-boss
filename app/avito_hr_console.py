"""Owner/admin API for vacancy facts and durable Avito handoff."""
import json

from aiohttp import web
from sqlalchemy import text

from .avito_dialogue import LABELS, TERM_FIELDS, respond, validate_terms
from .miniapp_security import AccessError


def require_hr(actor):
    if not set(actor.get('roles', ())) & {'OWNER', 'ADMIN'}:
        raise AccessError('HR доступен владельцу и администратору.', 403)


class Console:
    def __init__(self, api, worker, configured):
        self.api, self.worker, self.configured = api, worker, configured

    async def listing(self, request):
        require_hr(request['miniapp_actor'])
        async with self.api.engine.connect() as conn:
            vacancies = (await conn.execute(text('''SELECT v.code,v.role,v.item_id,v.url,v.status,
                t.payload,t.revision,t.confirmed FROM hr_avito_vacancies v
                LEFT JOIN hr_avito_terms t ON t.vacancy_code=v.code ORDER BY v.code'''))).mappings().all()
            control = (await conn.execute(text('SELECT paused,reason FROM hr_avito_control WHERE id=1'))).mappings().one()
            sessions = (await conn.execute(text('''SELECT s.*,EXISTS(SELECT 1 FROM hr_avito_outbox o
                WHERE o.chat_id=s.chat_id AND o.state='uncertain') AS uncertain_send FROM hr_avito_sessions s
                ORDER BY updated_at DESC,chat_id LIMIT 200'''))).mappings().all()
        return web.json_response({
            'configured': self.configured, 'workerReady': self.worker.ready,
            'lastCheck': self.worker.last_check, 'fields': list(TERM_FIELDS),
            'paused': bool(control['paused']), 'pauseReason': control['reason'],
            'vacancies': [dict(v, payload=json.loads(v['payload'] or '{}'),
                               revision=v['revision'] or 0, confirmed=bool(v['confirmed'])) for v in vacancies],
            'candidates': [dict(s, profile=json.loads(s['profile']),
                                transcript=json.loads(s['transcript']),
                                ignored_outgoing=None, updated_at=str(s['updated_at'])) for s in sessions],
            'profileLabels': LABELS,
            'note': 'Показаны последние 200 диалогов. Поиск резюме и публикация вакансий не запускаются этим кабинетом.'})

    async def terms(self, request):
        actor = request['miniapp_actor']
        require_hr(actor)
        body = await self.api.body(request, max_bytes=32000)
        if set(body) != {'revision', 'payload', 'confirmed'} or type(body['revision']) is not int or type(body['confirmed']) is not bool:
            raise AccessError('Проверьте поля условий вакансии.', 400)
        try:
            payload = validate_terms(body['payload'])
        except ValueError as error:
            raise AccessError(str(error), 400) from None
        if body['confirmed'] and not all(payload[k] for k in ('title', 'pay', 'schedule', 'location')):
            raise AccessError('Для подтверждения заполните название, оплату, график и место работы.', 400)
        code = request.match_info['code']
        async with self.api.engine.begin() as conn:
            if not await conn.scalar(text('SELECT count(*) FROM hr_avito_vacancies WHERE code=:code'), {'code': code}):
                raise AccessError('Вакансия не найдена.', 404)
            await conn.execute(text('INSERT INTO hr_avito_terms(vacancy_code) VALUES (:code) ON CONFLICT(vacancy_code) DO NOTHING'), {'code': code})
            result = await conn.execute(text('''UPDATE hr_avito_terms SET payload=:payload,
                revision=revision+1,confirmed=:confirmed WHERE vacancy_code=:code AND revision=:revision
                RETURNING revision'''), {'code': code, 'revision': body['revision'],
                'confirmed': int(body['confirmed']), 'payload': json.dumps(payload, ensure_ascii=False)})
            revision = result.scalar()
            if revision is None:
                raise AccessError('Условия уже изменены. Обновите кабинет.', 409)
            await self.api.audit_write(conn, actor, 'avito_terms_updated', 'avito_vacancy', 0,
                                       json.dumps({'code': code, 'revision': revision, 'confirmed': body['confirmed']}))
        return web.json_response({'ok': True, 'revision': revision})

    async def control(self, request):
        actor = request['miniapp_actor']
        require_hr(actor)
        body = await self.api.body(request)
        if set(body) != {'action'} or not isinstance(body['action'], str) or body['action'] not in {'pause', 'resume'}:
            raise AccessError('Выберите передачу менеджеру или возврат ассистенту.', 400)
        chat = request.match_info['chat']
        fresh = None
        if body['action'] == 'resume':
            if not self.configured:
                raise AccessError('Подключение Авито не настроено.', 409)
            if not getattr(self.worker, 'uid', None):
                self.worker.uid = (await self.worker.api.request('GET', '/core/v1/accounts/self'))['id']
            fresh = await self.worker.read_messages(chat)
        async with self.api.engine.begin() as conn:
            lock = ' FOR UPDATE' if conn.dialect.name == 'postgresql' else ''
            row = (await conn.execute(text('SELECT * FROM hr_avito_sessions WHERE chat_id=:chat' + lock), {'chat': chat})).mappings().first()
            if not row:
                raise AccessError('Диалог не найден.', 404)
            if body['action'] == 'resume' and row['status'] == 'STOPPED':
                raise AccessError('Кандидат отказался от общения. Автоответы не возобновлены.', 409)
            if body['action'] == 'resume' and await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE chat_id=:chat AND state='uncertain'"), {'chat': chat}):
                raise AccessError('Есть отправка с неизвестным результатом. Сначала проверьте её в Авито.', 409)
            ignored = [str(m['id']) for m in (fresh if fresh is not None else json.loads(row['transcript'])) if m.get('direction') == 'out']
            await conn.execute(text('''UPDATE hr_avito_sessions SET status=:status,
                ignored_outgoing=:ignored,updated_at=CURRENT_TIMESTAMP WHERE chat_id=:chat'''),
                {'chat': chat, 'status': 'HUMAN' if body['action'] == 'pause' else 'ACTIVE',
                 'ignored': json.dumps(ignored)})
            if body['action'] == 'resume':
                await conn.execute(text("UPDATE hr_avito_escalations SET reason='resolved_manager' WHERE chat_id=:chat"), {'chat': chat})
            await self.api.audit_write(conn, actor, 'avito_chat_' + body['action'], 'avito_chat', 0,
                                       json.dumps({'chat': chat}))
        return web.json_response({'ok': True})

    async def bind(self, request):
        actor = request['miniapp_actor']
        require_hr(actor)
        body = await self.api.body(request)
        if (set(body) != {'itemId', 'role'} or type(body['itemId']) is not int
                or not 0 < body['itemId'] < 2**63 or not isinstance(body['role'], str)
                or body['role'] not in {'PHOTOGRAPHER', 'MANAGER'}):
            raise AccessError('Укажите номер вакансии и должность.', 400)
        if not self.configured:
            raise AccessError('Сначала настройте подключение Авито на сервере.', 409)
        from .avito_hr import own_items
        items = await own_items(self.worker.api)
        item = next((item for item in items if str(item.get('id')) == str(body['itemId'])), None)
        if not item or item.get('status') != 'active':
            raise AccessError('Активная вакансия не найдена среди объявлений подключённого аккаунта.', 400)
        code = 'avito-item-' + str(body['itemId'])
        async with self.api.engine.begin() as conn:
            existing = await conn.scalar(text('SELECT code FROM hr_avito_vacancies WHERE item_id=:item'), {'item': body['itemId']})
            if existing:
                raise AccessError('Эта вакансия уже подключена. Измените её условия в кабинете.', 409)
            inserted = await conn.scalar(text('''INSERT INTO hr_avito_vacancies
                (code,role,payload_hash,status,item_id) VALUES (:code,:role,'bound-existing','activated',:item)
                ON CONFLICT(code) DO NOTHING RETURNING code'''), {'code': code, 'role': body['role'], 'item': body['itemId']})
            if not inserted:
                raise AccessError('Вакансия уже подключена.', 409)
            await self.api.audit_write(conn, actor, 'avito_vacancy_bound', 'avito_vacancy', 0,
                                       json.dumps({'code': code, 'itemId': body['itemId'], 'role': body['role']}))
        return web.json_response({'ok': True, 'code': code}, status=201)

    async def preview(self, request):
        require_hr(request['miniapp_actor'])
        body = await self.api.body(request, max_bytes=12000)
        if set(body) != {'code', 'text'} or not isinstance(body['code'], str) or not isinstance(body['text'], str) or not 1 <= len(body['text']) <= 2000:
            raise AccessError('Введите код вакансии и тестовый вопрос.', 400)
        async with self.api.engine.connect() as conn:
            vacancy = (await conn.execute(text('SELECT role,status FROM hr_avito_vacancies WHERE code=:code'), {'code': body['code']})).mappings().first()
        if vacancy is None:
            raise AccessError('Вакансия не найдена.', 404)
        turn = respond(body['text'], vacancy['role'], terms=await self.worker.terms(body['code']), active=vacancy['status']=='activated')
        return web.json_response({'answer': turn.answer, 'needsManager': turn.escalate,
                                  'status': turn.status, 'sent': False})


def install_console(app, api, worker, configured):
    console = Console(api, worker, configured)
    app.router.add_get('/api/miniapp/hr/avito', console.listing)
    app.router.add_put('/api/miniapp/hr/avito/vacancies/{code}/terms', console.terms)
    app.router.add_post('/api/miniapp/hr/avito/chats/{chat}/control', console.control)
    app.router.add_post('/api/miniapp/hr/avito/preview', console.preview)
    app.router.add_post('/api/miniapp/hr/avito/vacancies/bind', console.bind)
    return console
