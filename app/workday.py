"""Configurable work checks, existing shift closure notes and guest ratings."""
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .miniapp_security import AccessError, utc_bounds
from .models import utc_now
from .people import People, positive_id
from .services.insights import cents, parsed


class Workday:
    def __init__(self,api): self.api=api

    async def checklist(self,conn,roles):
        rows=await self.api.rows(conn,"SELECT key,value FROM settings WHERE key LIKE 'checklist:%' ORDER BY key")
        return [dict(parsed(r['value']),key=r['key']) for r in rows if parsed(r['value']).get('active',True) and set(parsed(r['value']).get('roles',[])) & set(roles)]

    async def state(self,request):
        a=request['miniapp_actor'];day=self.api.today();lo,hi=utc_bounds(day,day,self.api.tz)
        async with self.api.engine.connect() as conn:
            items=await self.checklist(conn,a['roles'])
            done={r['item_key']:bool(r['done']) for r in await self.api.rows(conn,'SELECT item_key,done FROM work_checklist_completions WHERE user_id=:uid AND shift_date=:day',uid=a['id'],day=day)}
            sale_rows=await self.api.rows(conn,'SELECT id,amount,payment_status FROM sales WHERE credited_user_id=:uid AND created_at>=:lo AND created_at<:hi',uid=a['id'],lo=lo,hi=hi)
            bookings=await self.api.rows(conn,'SELECT id,status FROM bookings WHERE (photographer_id=:uid OR manager_id=:uid) AND shoot_date=:day',uid=a['id'],day=day)
            close=await self.api.rows(conn,'SELECT status,ended_at,report_note,report_saved_at FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day',uid=a['id'],day=day)
        return web.json_response({'date':str(day),'items':[i|{'done':done.get(i['key'],False)} for i in items],
            'summary':{'sales':len(sale_rows),'revenue':sum(cents(r['amount']) for r in sale_rows),'unpaid':sum(r['payment_status']!='PAID' for r in sale_rows),'bookings':len(bookings),'cancelled':sum(b['status'] in ('CANCELLED','REJECTED') for b in bookings)},
            'sales':[{'id':r['id'],'amount':cents(r['amount'])} for r in sale_rows],
            'closed':bool(close and close[0]['status']=='FINISHED'),'note':close[0]['report_note'] or '' if close else '',
            'reportSavedAt':str(close[0]['report_saved_at']) if close and close[0]['report_saved_at'] else None,
            'canConfigure':bool({'OWNER','ADMIN'} & set(a['roles']))})

    async def configure(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        from .people import check_editor
        check_editor(a['roles'])
        if set(body)!={'key','title','roles','active'} or not isinstance(body['title'],str) or not 3<=len(body['title'].strip())<=200 or type(body['active']) is not bool or not isinstance(body['roles'],list) or not body['roles'] or any(r not in ('PHOTOGRAPHER','MANAGER','ADMIN','OWNER') for r in body['roles']): raise AccessError('Проверьте контрольный пункт и роли.',400)
        import re
        if not isinstance(body['key'],str) or not re.fullmatch(r'checklist:[a-z0-9-]{1,60}',body['key']): raise AccessError('Некорректный ключ пункта.',400)
        async with self.api.engine.begin() as conn:
            await People(self.api).current_editor(conn,a['id'])
            old=await self.api.rows(conn,'SELECT value FROM settings WHERE key=:key',key=body['key'])
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),{'key':body['key'],'value':json.dumps(body,ensure_ascii=False)})
            await self.api.audit_write(conn,a,'checklist_configured','setting',None,json.dumps({'before':parsed(old[0]['value']) if old else None,'after':body},ensure_ascii=False))
        return web.json_response({'ok':True})

    async def operation(self,request):
        """Same key + payload replays the result; different payload is a conflict."""
        a=request['miniapp_actor'];body=await self.api.body(request)
        return await self.execute(a, body)

    async def media(self, request):
        reader = await request.multipart()
        meta = await reader.next()
        if meta is None or meta.name != 'operation':
            raise AccessError('Не указана операция вложения.', 400)
        encoded = await meta.read_chunk(8192)
        if not meta.at_eof():
            raise AccessError('Слишком большие метаданные.', 413)
        try:
            body = json.loads(encoded)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AccessError('Некорректная операция.', 400) from exc
        part = await reader.next()
        if part is None or part.name != 'file':
            raise AccessError('Не найдено вложение.', 400)
        raw = bytearray()
        while not part.at_eof():
            raw.extend(await part.read_chunk(65536))
            if len(raw) > 20 * 1024 * 1024:
                raise AccessError('Фото должно быть не больше 20 МБ.', 413)
        if await reader.next() is not None:
            raise AccessError('Передавайте одно вложение за запрос.', 400)
        return await self.execute(request['miniapp_actor'], body, bytes(raw))

    async def execute(self, a, body, raw=None):
        try:
            return await self._execute(a, body, raw)
        except IntegrityError as exc:
            raise AccessError('Операция или вложение уже учтены. Обновите данные перед повтором.', 409) from exc

    async def _execute(self, a, body, raw=None):
        from .workflow import KINDS, Workflow
        if not isinstance(body, dict):
            raise AccessError('Некорректная операция.', 400)
        if set(body)!={'key','kind','date','data','actorId'} or not isinstance(body['key'],str) or not 16<=len(body['key'])<=80 or not isinstance(body['data'],dict): raise AccessError('Некорректная операция.',400)
        if not isinstance(body['kind'], str) or body['kind'] not in KINDS | {'checklist','shift_report','attendance'}: raise AccessError('Этот тип операции не поддерживает синхронизацию.',400)
        if type(body['actorId']) is not int or body['actorId'] != a['id']: raise AccessError('Аккаунт изменился. Операция принадлежит другому сотруднику.',403)
        payload = json.dumps(body,sort_keys=True,ensure_ascii=False).encode()
        digest=hashlib.sha256(payload + (b'\x00'+hashlib.sha256(raw).digest() if raw is not None else b'')).hexdigest()
        async with self.api.engine.begin() as conn:
            users=await self.api.rows(conn,'SELECT id,active FROM users WHERE id=:uid FOR UPDATE',uid=a['id'])
            if not users or not users[0]['active']: raise AccessError('Доступ отключён.',403)
            roles={r['role'] for r in await self.api.rows(conn,'SELECT role FROM user_roles WHERE user_id=:uid',uid=a['id'])}
            if not roles & {'OWNER','ADMIN','PHOTOGRAPHER','MANAGER'}: raise AccessError('Рабочая роль снята.',403)
            old=await self.api.rows(conn,'SELECT payload_hash,result FROM operation_requests WHERE user_id=:uid AND request_key=:key',uid=a['id'],key=body['key'])
            if old:
                if old[0]['payload_hash']!=digest: raise AccessError('Ключ уже использован для других данных.',409)
                return web.json_response(parsed(old[0]['result']))
            day=self.api.day(body['date'])
            if not self.api.today()-timedelta(days=7)<=day<=self.api.today(): raise AccessError('Дата требует ручной проверки; операция не применена.',409)
            data=body['data']
            extra = {}
            if body['kind'] == 'attendance':
                from .attendance import Attendance
                extra = await Attendance(self.api).offline(conn, a | {'roles': sorted(roles)}, day, data, raw)
            elif body['kind'] in KINDS:
                extra = await Workflow(self.api).apply(conn, a | {'roles': sorted(roles)}, body['kind'], data, raw)
            elif raw is not None:
                raise AccessError('Вложение не поддерживается для этой операции.', 400)
            elif body['kind']=='checklist':
                if set(data)!={'itemKey','done'} or type(data['done']) is not bool or not isinstance(data['itemKey'],str) or data['itemKey'] not in {i['key'] for i in await self.checklist(conn,roles)}: raise AccessError('Пункт больше не доступен вашей роли.',409)
                await conn.execute(text('''INSERT INTO work_checklist_completions(user_id,shift_date,item_key,done,updated_at) VALUES (:uid,:day,:item,:done,:now)
                    ON CONFLICT(user_id,shift_date,item_key) DO UPDATE SET done=excluded.done,updated_at=excluded.updated_at'''),{'uid':a['id'],'day':day,'item':data['itemKey'],'done':data['done'],'now':utc_now()})
            else:
                if set(data)!={'note','expectedSavedAt'} or not isinstance(data['note'],str) or len(data['note'])>2000: raise AccessError('Замечание — до 2000 символов.',400)
                outs=await self.api.rows(conn,"SELECT id,report_note,report_saved_at FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day AND status='FINISHED' FOR UPDATE",uid=a['id'],day=day)
                if not outs: raise AccessError('Сначала подтвердите окончание смены геолокацией и фото.',409)
                current=str(outs[0]['report_saved_at']) if outs[0]['report_saved_at'] else None
                if data['expectedSavedAt']!=current: raise AccessError('Отчёт уже изменён. Обновите его перед сохранением.',409)
                await conn.execute(text('UPDATE shift_check_outs SET report_note=:note,report_saved_at=:now WHERE id=:id'),{'note':data['note'].strip(),'now':utc_now(),'id':outs[0]['id']})
                await self.api.audit_write(conn,a,'shift_report_saved','shift_check_out',outs[0]['id'],json.dumps({'before':outs[0]['report_note'],'after':data['note'].strip()},ensure_ascii=False))
            result={'ok':True,'status':'synced','key':body['key'],**extra}
            await conn.execute(text('INSERT INTO operation_requests(user_id,request_key,payload_hash,result,created_at) VALUES (:uid,:key,:hash,:result,:now)'),{'uid':a['id'],'key':body['key'],'hash':digest,'result':json.dumps(result),'now':utc_now()})
        return web.json_response(result)

    def token(self,sale_id):
        # Domain-separated capability; no Telegram or other credential is exposed.
        digest=hmac.new(self.api.bot.token.encode(),f'photo-boss:guest-feedback:v1:{sale_id}'.encode(),hashlib.sha256).hexdigest()
        return f'{sale_id}.{digest}'

    async def feedback_link(self,request):
        a=request['miniapp_actor'];sid=positive_id(request.match_info['id'])
        async with self.api.engine.begin() as conn:
            rows=await self.api.rows(conn,'SELECT s.id,s.credited_user_id,b.manager_id FROM sales s LEFT JOIN bookings b ON b.id=s.booking_id WHERE s.id=:id',id=sid)
            if not rows: raise AccessError('Продажа не найдена.',404)
            if not {'OWNER','ADMIN'} & set(a['roles']) and a['id'] not in (rows[0]['credited_user_id'],rows[0]['manager_id']): raise AccessError('Чужая продажа.',403)
            token=self.token(sid);digest=hashlib.sha256(token.encode()).hexdigest()
            await conn.execute(text("INSERT INTO guest_feedback(sale_id,token_hash,comment,created_at) VALUES (:sid,:hash,'',:now) ON CONFLICT(sale_id) DO NOTHING"),{'sid':sid,'hash':digest,'now':utc_now()})
        return web.json_response({'path':'/feedback/'+token,'note':'Передайте ссылку гостю после продажи. Одна оценка на продажу; срок ссылки — 14 дней.'})

    async def feedback_page(self,request):
        return web.FileResponse(Path(__file__).parent/'webapp'/'feedback.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"})

    async def feedback_submit(self,request):
        token=request.match_info['token']
        if len(token)>100: raise web.HTTPNotFound()
        try:
            body=await self.api.body(request)
            if set(body)!={'rating','comment'} or type(body['rating']) is not int or not 1<=body['rating']<=5 or not isinstance(body['comment'],str) or len(body['comment'])>1000: raise AccessError('Оценка от 1 до 5, комментарий до 1000 символов.',400)
            async with self.api.engine.begin() as conn:
                rows=await self.api.rows(conn,'''UPDATE guest_feedback SET rating=:rating,comment=:comment,submitted_at=:now
                    WHERE token_hash=:hash AND rating IS NULL AND created_at>=:cutoff RETURNING id''',rating=body['rating'],comment=body['comment'].strip(),now=utc_now(),hash=hashlib.sha256(token.encode()).hexdigest(),cutoff=utc_now()-timedelta(days=14))
                if not rows: raise AccessError('Ссылка недействительна, истекла или оценка уже отправлена.',409)
            return web.json_response({'ok':True})
        except AccessError as e: return web.json_response({'error':str(e)},status=e.status)


def install_workday(app,api):
    service=Workday(api)
    from .workflow import Workflow
    for method,path,handler in [('GET','/workflow',Workflow(api).listing),('GET','/workday',service.state),('PUT','/workday/checklist',service.configure),('POST','/operations/sync',service.operation),('POST','/operations/media',service.media),('POST','/sales/{id}/feedback-link',service.feedback_link)]:
        app.router.add_route(method,'/api/miniapp'+path,handler)
    app.router.add_get('/feedback/{token}',service.feedback_page)
    app.router.add_post('/feedback/{token}',service.feedback_submit)
    return service
