"""Own-user, immutable booking diagnostic receipts without external AI calls."""
import hashlib
import json
import re

from aiohttp import web
from sqlalchemy import text

from .miniapp_security import STAFF_ROLES, AccessError
from .models import utc_now
from .services.booking_diagnostic import VERSION, assess, public_questions

NAMESPACE = 'academy-booking-diagnostic:v1:'


async def load_diagnostic(api, conn, uid):
    rows = await api.rows(conn, '''SELECT result FROM operation_requests
        WHERE user_id=:uid AND request_key LIKE :prefix ORDER BY id DESC LIMIT 1''',
        uid=uid, prefix=NAMESPACE + '%')
    if not rows:
        return None
    try:
        data = json.loads(rows[0]['result'])
        result = data.get('assessment')
        return result if isinstance(result, dict) and result.get('version') == VERSION else None
    except (ValueError, TypeError, AttributeError):
        return None


class AcademyDiagnostic:
    def __init__(self, api):
        self.api, self.engine = api, api.engine

    @staticmethod
    def actor(request):
        actor = request['miniapp_actor']
        if not STAFF_ROLES & set(actor['roles']):
            raise AccessError('Входная диагностика доступна сотрудникам Академии.', 403)
        return actor

    async def listing(self, request):
        actor = self.actor(request)
        async with self.engine.connect() as conn:
            latest = await load_diagnostic(self.api, conn, actor['id'])
        return web.json_response({'version': VERSION, 'questions': public_questions(), 'assessment': latest})

    async def submit(self, request):
        actor = self.actor(request)
        body = await self.api.body(request, max_bytes=8192)
        if not isinstance(body, dict) or set(body) != {'answers', 'requestId'}:
            raise AccessError('Отправьте ответы и идентификатор попытки.', 400)
        request_id = body['requestId']
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{12,48}', request_id):
            raise AccessError('Неверный идентификатор попытки.', 400)
        try:
            assessment = assess(body['answers'])
        except ValueError as exc:
            raise AccessError(str(exc), 400) from exc
        key = NAMESPACE + request_id
        payload = json.dumps({'version': VERSION, 'answers': body['answers']}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = utc_now()
        assessment |= {'requestId': request_id, 'createdAt': now.isoformat()}
        receipt = json.dumps({'assessment': assessment, 'answers': body['answers']}, ensure_ascii=False)
        async with self.engine.begin() as conn:
            result = await conn.execute(text('''INSERT INTO operation_requests
                (user_id,request_key,payload_hash,result,created_at) VALUES (:uid,:key,:hash,:result,:now)
                ON CONFLICT(user_id,request_key) DO NOTHING RETURNING id'''),
                {'uid': actor['id'], 'key': key, 'hash': digest, 'result': receipt, 'now': now})
            inserted = result.scalar_one_or_none()
            saved = await self.api.rows(conn, '''SELECT payload_hash,result FROM operation_requests
                WHERE user_id=:uid AND request_key=:key''', uid=actor['id'], key=key)
            if saved[0]['payload_hash'] != digest:
                raise AccessError('Попытка с этим идентификатором уже сохранена с другими ответами.', 409)
            stored = json.loads(saved[0]['result'])['assessment']
        return web.json_response({'assessment': stored, 'replayed': inserted is None})


def install_academy_diagnostic(app, api):
    service = AcademyDiagnostic(api)
    app['academy_diagnostic'] = service
    prefix = '/api/miniapp/academy/diagnostic'
    app.router.add_get(prefix, service.listing)
    app.router.add_post(prefix, service.submit)
    return service
