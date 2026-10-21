"""Configurable work checks, existing shift closure notes and guest ratings."""
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from pathlib import Path

from aiohttp import ClientPayloadError, web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .miniapp_security import AccessError, utc_bounds
from .models import utc_now
from .operations_guard import require_generation
from .people import People, positive_id
from .services.booking_access import booking_scope_sql
from .services.insights import cents, parsed

logger = logging.getLogger(__name__)


def operation_diagnostic(body, *, result=None, error=None):
    """Log only known operation fields; never file bytes, names or request keys."""
    kinds = {'sale_start', 'sale_receipt', 'sale_counts', 'sale_selected', 'sale_disk_selected', 'sale_complete',
             'shoot_photo', 'shoot_complete', 'shoot_retry', 'booking_receipt'}
    if not isinstance(body, dict) or not isinstance(body.get('kind'), str) or body['kind'] not in kinds:
        return
    detail = {'kind': body['kind']}
    if isinstance(result, dict):
        for field in ('bookingId', 'draftId', 'shootingId', 'photoId', 'saleId',
                      'selectedPhotos', 'declaredSold', 'declaredTotal'):
            value = result.get(field)
            if type(value) is int:
                detail[field] = value
        detail['duplicate'] = result.get('duplicate') is True
        logger.info('Operation accepted: %s', json.dumps(detail, sort_keys=True))
    if error is not None:
        message = str(error)
        reason = ('full_count_changed' if message.startswith('Полная съёмка содержит ') else
                  'selected_photos_missing' if message.startswith('Сначала загрузите все выбранные фотографии:') else
                  'selected_photo_already_sold' if message.startswith('Этот кадр уже куплен') else
                  'price_changed' if message.startswith('Цена пакета изменилась') else
                  'storage_unavailable' if 'Яндекс.Диск' in message else
                  'dependency_pending' if getattr(error, 'status', 0) == 424 else
                  'access_denied' if getattr(error, 'status', 0) == 403 else 'validation_rejected')
        detail.update(status=getattr(error, 'status', 409), reason=reason)
        logger.info('Operation rejected: %s', json.dumps(detail, sort_keys=True))


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
            condition = booking_scope_sql(a['id'], a['roles'])
            bookings=await self.api.rows(conn,f'SELECT b.id,b.status FROM bookings b WHERE ({condition}) AND b.shoot_date=:day',uid=a['id'],day=day)
            close=await self.api.rows(conn,'SELECT status,ended_at,report_note,report_saved_at FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day',uid=a['id'],day=day)
            config_items = [parsed(r['value'])|{'key':r['key']} for r in await self.api.rows(conn,"SELECT key,value FROM settings WHERE key LIKE 'checklist:%' ORDER BY key")] if {'OWNER','ADMIN'} & set(a['roles']) else []
        return web.json_response({'date':str(day),'timezone':str(self.api.tz),'items':[i|{'done':done.get(i['key'],False)} for i in items],
            'summary':{'sales':len(sale_rows),'revenue':sum(cents(r['amount']) for r in sale_rows),'unpaid':sum(r['payment_status']!='PAID' for r in sale_rows),'bookings':len(bookings),'cancelled':sum(b['status'] in ('CANCELLED','REJECTED') for b in bookings)},
            'sales':[{'id':r['id'],'amount':cents(r['amount'])} for r in sale_rows],
            'closed':bool(close and close[0]['status']=='FINISHED'),'note':close[0]['report_note'] or '' if close else '',
            'canReport':bool(close and close[0]['status'] in ('FINISHED','PENDING_REVIEW')),
            'configItems':config_items,
            'reportSavedAt':str(close[0]['report_saved_at']) if close and close[0]['report_saved_at'] else None,
            'canConfigure':bool({'OWNER','ADMIN'} & set(a['roles']))})

    async def configure(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        from .people import check_editor
        check_editor(a['roles'])
        if set(body) not in ({'key','title','roles','active'},{'key','title','roles','active','required'}) or not isinstance(body['title'],str) or not 3<=len(body['title'].strip())<=200 or type(body['active']) is not bool or type(body.get('required',False)) is not bool or not isinstance(body['roles'],list) or not body['roles'] or any(r not in ('PHOTOGRAPHER','MANAGER','ADMIN','OWNER') for r in body['roles']): raise AccessError('Проверьте контрольный пункт и роли.',400)
        body = body | {'required':body.get('required',False)}
        import re
        if not isinstance(body['key'],str) or not re.fullmatch(r'checklist:[a-z0-9-]{1,60}',body['key']): raise AccessError('Некорректный ключ пункта.',400)
        async with self.api.engine.begin() as conn:
            await People(self.api).current_editor(conn,a['id'])
            old=await self.api.rows(conn,'SELECT value FROM settings WHERE key=:key',key=body['key'])
            previous = parsed(old[0]['value']) if old else {}
            same_rule = previous.get('active') and all(previous.get(key)==body.get(key) for key in ('title','roles','required'))
            body['effectiveFrom'] = previous.get('effectiveFrom',str(self.api.today())) if same_rule else str(self.api.today())
            await conn.execute(text('INSERT INTO settings(key,value) VALUES (:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),{'key':body['key'],'value':json.dumps(body,ensure_ascii=False)})
            await self.api.audit_write(conn,a,'checklist_configured','setting',None,json.dumps({'before':parsed(old[0]['value']) if old else None,'after':body},ensure_ascii=False))
        return web.json_response({'ok':True})

    async def operation(self,request):
        """Same key + payload replays the result; different payload is a conflict."""
        a=request['miniapp_actor'];body=await self.api.body(request)
        return await self.execute(a, body)

    async def media(self, request):
        try:
            return await self.read_media(request)
        except AccessError:
            # AccessError is a ValueError: do not turn a retryable 503 or access
            # denial into a permanent 400 in the browser's persistent outbox.
            raise
        except (ConnectionError, ClientPayloadError, TimeoutError) as exc:
            raise AccessError('Передача файла прервалась. Повторите отправку; не удаляйте исходный файл.', 503) from exc
        except (ValueError, AssertionError) as exc:
            raise AccessError('Не удалось прочитать вложение. Повторите отправку; если ошибка повторится, выберите файл заново.', 400) from exc

    async def read_media(self, request):
        reader = await request.multipart()
        meta = await reader.next()
        if meta is None or meta.name != 'operation':
            raise AccessError('Не указана операция вложения.', 400)
        encoded = bytearray()
        while not meta.at_eof():
            chunk = await meta.read_chunk(8192)
            if not chunk and not meta.at_eof():
                raise ConnectionResetError('Incomplete operation metadata')
            encoded.extend(chunk)
            if len(encoded) > 8192:
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
            chunk = await part.read_chunk(65536)
            if not chunk and not part.at_eof():
                raise ConnectionResetError('Incomplete file payload')
            raw.extend(chunk)
            if len(raw) > 20 * 1024 * 1024:
                raise AccessError('Фото должно быть не больше 20 МБ.', 413)
        if await reader.next() is not None:
            raise AccessError('Передавайте одно вложение за запрос.', 400)
        return await self.execute(request['miniapp_actor'], body, bytes(raw))

    async def execute(self, a, body, raw=None):
        try:
            response = await self._execute(a, body, raw)
            operation_diagnostic(body, result=json.loads(response.text))
            return response
        except AccessError as exc:
            operation_diagnostic(body, error=exc)
            raise
        except IntegrityError as exc:
            operation_diagnostic(body, error=AccessError('Conflict', 409))
            raise AccessError('Операция или вложение уже учтены. Обновите данные перед повтором.', 409) from exc

    async def _execute(self, a, body, raw=None):
        from .workflow import KINDS, Workflow
        if not isinstance(body, dict):
            raise AccessError('Некорректная операция.', 400)
        required = {'key','kind','date','data','actorId'}
        if (not required <= set(body) or set(body) - required - {'generation', 'salesGeneration'}
                or not isinstance(body['key'],str) or not 16<=len(body['key'])<=80
                or not isinstance(body['data'],dict)
                or ('generation' in body and not isinstance(body['generation'], str))
                or ('salesGeneration' in body and not isinstance(body['salesGeneration'], str))):
            raise AccessError('Некорректная операция.',400)
        if body['key'].startswith(('academy-', 'mentor-', 'realtime-')):
            raise AccessError('Некорректный идентификатор операции.', 400)
        if not isinstance(body['kind'], str) or body['kind'] not in KINDS | {'checklist','shift_report','attendance','cash_expense','break_start','break_end'}: raise AccessError('Этот тип операции не поддерживает синхронизацию.',400)
        if type(body['actorId']) is not int or body['actorId'] != a['id']: raise AccessError('Аккаунт изменился. Операция принадлежит другому сотруднику.',403)
        payload = json.dumps(body,sort_keys=True,ensure_ascii=False).encode()
        digest=hashlib.sha256(payload + (b'\x00'+hashlib.sha256(raw).digest() if raw is not None else b'')).hexdigest()
        async with self.api.engine.begin() as conn:
            # Never acknowledge a pre-reset operation, even if its key remains
            # in the shared idempotency ledger. Manual cash outflows survive
            # the reset and retain their original payload hash and replay.
            if body['kind'] != 'cash_expense':
                await require_generation(conn, body.get('generation', ''))
            if body['kind'].startswith('sale_') or body['kind'] == 'booking':
                sales_generation = await conn.scalar(text("SELECT value FROM settings WHERE key='sales:generation'"))
                if sales_generation and body.get('salesGeneration', '') != sales_generation:
                    raise AccessError('Продажи обнулены владельцем. Обновите приложение и начните продажу заново; старые файлы сохранены.', 409)
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
            if body['kind'] == 'cash_expense':
                from .cash_control import CashControl
                if raw is not None:
                    raise AccessError('Вложение в этой операции не поддерживается.',400)
                extra=await CashControl(self.api).record(conn,a|{'roles':sorted(roles)},data)
            elif body['kind'] in {'break_start', 'break_end'}:
                from .work_breaks import WorkBreaks
                extra = await WorkBreaks(self.api).record(conn, a | {'roles': sorted(roles)}, day, body['kind'], data)
            elif body['kind'] == 'attendance':
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
                outs=await self.api.rows(conn,"SELECT id,report_note,report_saved_at FROM shift_check_outs WHERE user_id=:uid AND shift_date=:day AND status IN ('FINISHED','PENDING_REVIEW') FOR UPDATE",uid=a['id'],day=day)
                if not outs: raise AccessError('Сначала сохраните окончание смены с геолокацией и фото.',409)
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
            if 'ADMIN' in a['roles'] and 'OWNER' not in a['roles']:
                allowed = await self.api.rows(conn, 'SELECT s.id FROM sales s JOIN bookings b ON b.id=s.booking_id WHERE s.id=:id AND ('+booking_scope_sql(a['id'], a['roles'])+')', id=sid, uid=a['id'])
                if not allowed: raise AccessError('Нет доступа к съёмке этого отеля.',403)
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
    from .push_notifications import install_push
    install_push(app, api)
    from .hotel_pricing import HotelPricing
    from .work_breaks import install_breaks
    install_breaks(app, api)
    from .bank_payments import install_bank_payments
    install_bank_payments(app, api)
    app.router.add_post('/api/miniapp/hotel-tariffs', HotelPricing(api).save)
    from .services.sale_discounts import SaleDiscounts
    app.router.add_post('/api/miniapp/sale-discounts', SaleDiscounts(api).request)
    app.router.add_post('/api/miniapp/sale-discounts/{id}/decision', SaleDiscounts(api).decide)
    from .cash_control import install_cash_control
    install_cash_control(app,api)
    from .photo_edits import PhotoFiles
    from .workflow import Workflow
    app.router.add_get('/api/miniapp/photos/{photo}/image', PhotoFiles(api).image)
    app.router.add_get('/api/miniapp/receipts/{id}/image', Workflow(api).receipt_image)
    app.router.add_get('/api/miniapp/sale-drafts/{id}/receipt/image', Workflow(api).draft_receipt_image)
    for method,path,handler in [('GET','/workflow',Workflow(api).listing),('GET','/workday',service.state),('PUT','/workday/checklist',service.configure),('POST','/operations/sync',service.operation),('POST','/operations/media',service.media),('POST','/sales/{id}/feedback-link',service.feedback_link)]:
        app.router.add_route(method,'/api/miniapp'+path,handler)
    app.router.add_get('/feedback/{token}',service.feedback_page)
    app.router.add_post('/feedback/{token}',service.feedback_submit)
    return service
