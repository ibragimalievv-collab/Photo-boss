"""Recruitment and employee overview, extending existing People and Academy."""
import json
from datetime import datetime, timedelta, timezone

from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .miniapp_security import AccessError, utc_bounds
from .models import utc_now
from .people import People, check_editor, positive_id
from .services.insights import cents, parsed
from .services.onboarding import QUIZ, guides

STAGES = {'NEW':'Новый','CONTACTED':'Общение','INTERVIEW':'Интервью','OFFER':'Предложение','DOCUMENTS':'Оформление','HIRED':'Принят','REJECTED':'Отказ'}
TRANSITIONS = {'NEW':{'CONTACTED','REJECTED'},'CONTACTED':{'INTERVIEW','REJECTED'},'INTERVIEW':{'OFFER','REJECTED'},'OFFER':{'DOCUMENTS','REJECTED'},'DOCUMENTS':{'REJECTED'},'REJECTED':{'NEW'},'HIRED':set()}


def field(body, key, maximum, *, required=False):
    value=body.get(key, '')
    if not isinstance(value,str) or len(value)>maximum or (required and not value.strip()):
        raise AccessError(f'Проверьте поле {key}.',400)
    return value.strip()


def candidate(row):
    return {k:(str(v) if isinstance(v,datetime) else v) for k,v in row.items() if k!='notes'} | {'notes':json.loads(row['notes'] or '[]')}


class Team:
    def __init__(self,api):
        self.api=api
        self.people=People(api)

    async def listing(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        async with self.api.engine.connect() as conn:
            rows=await self.api.rows(conn,'SELECT * FROM hr_candidates ORDER BY updated_at DESC,id DESC LIMIT 300')
            staff=await self.api.rows(conn,'SELECT id,name,active FROM users ORDER BY name')
        return web.json_response({'items':[candidate(r) for r in rows],'stages':STAGES,'employees':[dict(r) for r in staff]})

    async def create(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        body=await self.api.body(request)
        if set(body)!={'name','contact','source','role'} or body.get('role') not in {'PHOTOGRAPHER','MANAGER'}:
            raise AccessError('Заполните имя, контакт, источник и рабочую роль.',400)
        values={k:field(body,k,n,required=True) for k,n in [('name',150),('contact',300),('source',150)]}
        async with self.api.engine.begin() as conn:
            await self.people.current_editor(conn,actor['id'])
            row=(await self.api.rows(conn,'''INSERT INTO hr_candidates(name,contact,source,role,stage,decision,notes,created_by_id,revision,created_at,updated_at)
                VALUES (:name,:contact,:source,:role,'NEW','','[]',:actor,1,:now,:now) RETURNING id''',**values,role=body['role'],actor=actor['id'],now=utc_now()))[0]
            await self.api.audit_write(conn,actor,'candidate_created','hr_candidate',row['id'],json.dumps({'before':None,'after':values},ensure_ascii=False))
        return web.json_response({'id':row['id']},status=201)

    async def update(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        cid=positive_id(request.match_info['id']);body=await self.api.body(request)
        if set(body)!={'revision','stage','interviewAt','decision','note'} or type(body['revision']) is not int or body['stage'] not in STAGES:
            raise AccessError('Обновите карточку кандидата.',400)
        note=field(body,'note',2000);decision=field(body,'decision',2000)
        interview=None
        if body['interviewAt']:
            try:
                interview=datetime.fromisoformat(body['interviewAt'])
                if interview.tzinfo is None: raise ValueError
                interview=interview.astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError,TypeError):
                raise AccessError('Время интервью должно включать часовой пояс.',400) from None
        async with self.api.engine.begin() as conn:
            await self.people.current_editor(conn,actor['id'])
            rows=await self.api.rows(conn,'SELECT * FROM hr_candidates WHERE id=:id FOR UPDATE',id=cid)
            if not rows: raise AccessError('Кандидат не найден.',404)
            old=rows[0]
            if old['revision']!=body['revision']: raise AccessError('Карточка изменена другим сотрудником. Обновите её.',409)
            stage=body['stage']
            if stage!=old['stage'] and stage not in TRANSITIONS[old['stage']]: raise AccessError('Сначала завершите предыдущий этап. Найм выполняется отдельной кнопкой.',409)
            if stage=='INTERVIEW' and interview is None: raise AccessError('Укажите время интервью.',400)
            if stage in {'OFFER','DOCUMENTS','REJECTED'} and not decision: raise AccessError('Запишите решение.',400)
            notes=json.loads(old['notes'] or '[]')
            if note:
                if len(notes)>=200: raise AccessError('В карточке достигнут предел 200 заметок.',409)
                notes.append({'actorId':actor['id'],'at':str(utc_now()),'text':note})
            new={'stage':stage,'interview_at':str(interview) if interview else None,'decision':decision}
            await conn.execute(text('''UPDATE hr_candidates SET stage=:stage,interview_at=:interview,decision=:decision,notes=:notes,revision=revision+1,updated_at=:now WHERE id=:id'''),
                {'stage':stage,'interview':interview,'decision':decision,'notes':json.dumps(notes,ensure_ascii=False),'now':utc_now(),'id':cid})
            await self.api.audit_write(conn,actor,'candidate_updated','hr_candidate',cid,json.dumps({'before':{k:str(old[k]) if old[k] is not None else None for k in new},'after':new,'noteAdded':bool(note)},ensure_ascii=False))
        return web.json_response({'ok':True})

    async def hire(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        cid=positive_id(request.match_info['id']);body=await self.api.body(request)
        if set(body)!={'revision','telegramId','hotelIds'} or type(body['revision']) is not int or not isinstance(body['hotelIds'],list) or len(body['hotelIds'])>50:
            raise AccessError('Проверьте данные найма.',400)
        tg=positive_id(body['telegramId'],maximum=2**52-1);hotels=sorted({positive_id(h) for h in body['hotelIds']})
        try:
            async with self.api.engine.begin() as conn:
                await self.people.current_editor(conn,actor['id'])
                rows=await self.api.rows(conn,'SELECT * FROM hr_candidates WHERE id=:id FOR UPDATE',id=cid)
                if not rows: raise AccessError('Кандидат не найден.',404)
                c=rows[0]
                if c['employee_id']: return web.json_response({'employeeId':c['employee_id'],'alreadyHired':True})
                if c['revision']!=body['revision'] or c['stage']!='DOCUMENTS': raise AccessError('Сначала завершите интервью, решение и оформление.',409)
                await self.people.verify_hotels(conn,hotels)
                users=await self.api.rows(conn,'SELECT id,active FROM users WHERE tg_id=:tg FOR UPDATE',tg=tg)
                if users:
                    uid=users[0]['id']
                    roles={r['role'] for r in await self.api.rows(conn,'SELECT role FROM user_roles WHERE user_id=:id',id=uid)}
                    if not users[0]['active'] or roles: raise AccessError('Telegram ID уже принадлежит сотруднику. Используйте существующую карточку.',409)
                else:
                    uid=(await self.api.rows(conn,'INSERT INTO users(tg_id,name,active,created_at) VALUES (:tg,:name,TRUE,:now) RETURNING id',tg=tg,name=c['name'],now=utc_now()))[0]['id']
                await self.people.assignments(conn,uid,{'roles':[c['role']],'hotelIds':hotels})
                await conn.execute(text("UPDATE hr_candidates SET employee_id=:uid,stage='HIRED',revision=revision+1,updated_at=:now WHERE id=:id"),{'uid':uid,'now':utc_now(),'id':cid})
                await self.api.audit_write(conn,actor,'candidate_hired','hr_candidate',cid,json.dumps({'before':{'stage':c['stage'],'employeeId':None},'after':{'stage':'HIRED','employeeId':uid}},ensure_ascii=False))
        except IntegrityError as exc:
            raise AccessError('Этот сотрудник уже связан с кандидатом. Обновите карточку.',409) from exc
        return web.json_response({'employeeId':uid})

    async def onboarding_data(self,conn,uid,roles):
        steps=guides(roles)
        completed={r['topic_slug'] for r in await self.api.rows(conn,'SELECT topic_slug FROM academy_lesson_progress WHERE user_id=:uid',uid=uid)}
        attempts=await self.api.rows(conn,"SELECT score,result,created_at FROM academy_assessments WHERE user_id=:uid AND kind='entry-v1' ORDER BY id DESC LIMIT 5",uid=uid)
        practices=await self.api.rows(conn,"SELECT id,status,ai_score FROM training_assignments WHERE user_id=:uid ORDER BY id DESC LIMIT 30",uid=uid)
        return {'steps':[s|{'completed':s['slug'] in completed} for s in steps],
                'quiz':[{'id':q['id'],'question':q['question'],'choices':q['choices']} for q in QUIZ],
                'attempts':[{'score':a['score'],'result':parsed(a['result']),'at':str(a['created_at'])} for a in attempts],
                'academyLessons':sum(not slug.startswith('guide-') for slug in completed),
                'practices':[dict(p) for p in practices],
                'tasks':['Пройти инструкции своей роли','Сдать начальный тест: не менее 75%','Завершить первые четыре урока Академии','Сдать первую практику в Академии']}

    async def onboarding(self,request):
        a=request['miniapp_actor']
        async with self.api.engine.connect() as conn: data=await self.onboarding_data(conn,a['id'],a['roles'])
        return web.json_response(data)

    async def progress(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        if set(body)!={'slug'} or body['slug'] not in {s['slug'] for s in guides(a['roles'])}: raise AccessError('Инструкция недоступна вашей роли.',403)
        async with self.api.engine.begin() as conn:
            await conn.execute(text('''INSERT INTO academy_lesson_progress(user_id,topic_slug,completed_at) VALUES (:uid,:slug,:now)
                ON CONFLICT(user_id,topic_slug) DO NOTHING'''),{'uid':a['id'],'slug':body['slug'],'now':utc_now()})
        return web.json_response({'ok':True})

    async def assessment(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        answers=body.get('answers')
        if set(body)!={'answers'} or not isinstance(answers,dict) or set(answers)!={q['id'] for q in QUIZ} or any(type(answers[q['id']]) is not int or not 0<=answers[q['id']]<len(q['choices']) for q in QUIZ): raise AccessError('Ответьте на каждый вопрос.',400)
        errors=[{'question':q['question'],'tip':q['tip']} for q in QUIZ if answers[q['id']]!=q['answer']]
        score=round((len(QUIZ)-len(errors))/len(QUIZ)*100)
        result={'errors':errors,'passed':score>=75}
        async with self.api.engine.begin() as conn:
            await conn.execute(text("INSERT INTO academy_assessments(user_id,kind,score,result,created_at) VALUES (:uid,'entry-v1',:score,:result,:now)"),{'uid':a['id'],'score':score,'result':json.dumps(result,ensure_ascii=False),'now':utc_now()})
        return web.json_response({'score':score,**result})

    async def overview(self,request):
        a=request['miniapp_actor'];uid=positive_id(request.match_info['id'])
        if uid!=a['id']: check_editor(a['roles'])
        end=self.api.today();start=end-timedelta(days=29)
        async with self.api.engine.connect() as conn:
            profile=await self.people.card(conn,uid)
            # Telegram ID is visible only to the employee and staff administrators.
            data=await self.onboarding_data(conn,uid,profile['roles'])
            ins=await self.api.rows(conn,'SELECT shift_date,started_at,late,fine_amount,status FROM shift_check_ins WHERE user_id=:uid AND shift_date>=:start AND shift_date<=:end',uid=uid,start=start,end=end)
            outs=await self.api.rows(conn,'SELECT shift_date,ended_at,status FROM shift_check_outs WHERE user_id=:uid AND shift_date>=:start AND shift_date<=:end',uid=uid,start=start,end=end)
            lo,hi=utc_bounds(start,end,self.api.tz)
            plans=await self.api.rows(conn,"SELECT id,start_at,end_at,status,hotel_id FROM shifts WHERE user_id=:uid AND start_at>=:lo AND start_at<:hi AND status<>'CANCELLED'",uid=uid,lo=lo,hi=hi)
            confirmed={str(i['shift_date']) for i in ins if i['status']=='STARTED'}
            missed=[]
            from .miniapp_api import as_utc
            for p in plans:
                if as_utc(p['end_at']).replace(tzinfo=None)<utc_now() and str(as_utc(p['start_at']).astimezone(self.api.tz).date()) not in confirmed: missed.append(p['id'])
            fstart=end if 'ADMIN' in a['roles'] and 'OWNER' not in a['roles'] else start
            flo,fhi=utc_bounds(fstart,end,self.api.tz)
            sales=await self.api.rows(conn,'SELECT id,amount,commission,created_at FROM sales WHERE credited_user_id=:uid AND created_at>=:lo AND created_at<:hi',uid=uid,lo=flo,hi=fhi)
            pay=await self.api.rows(conn,'SELECT kind,amount,note,created_at FROM payroll_entries WHERE user_id=:uid AND created_at>=:lo AND created_at<:hi',uid=uid,lo=flo,hi=fhi)
            hotels=await self.api.rows(conn,'SELECT h.id,h.name FROM hotels h JOIN hotel_employees he ON he.hotel_id=h.id WHERE he.user_id=:uid',uid=uid)
        return web.json_response({'profile':profile,'hotels':[dict(h) for h in hotels],'from':str(start),'to':str(end),
            'discipline':{'late':sum(bool(i['late']) for i in ins),'missed':len(missed),'missedShiftIds':missed,'confirmed':len(confirmed),'checkIns':[{k:str(v) if isinstance(v,datetime) else v for k,v in i.items()}|{'shift_date':str(i['shift_date'])} for i in ins],'closed':sum(o['status']=='FINISHED' for o in outs)},
            'finance':{'from':str(fstart),'to':str(end),'revenue':sum(cents(s['amount']) for s in sales),'sales':len(sales),'commission':sum(cents(s['commission']) for s in sales),'adjustments':sum(cents(p['amount']) for p in pay),'entries':[{'kind':p['kind'],'amount':cents(p['amount']),'note':p['note'],'at':str(p['created_at'])} for p in pay]},'onboarding':data})


def install_team(app,api):
    service=Team(api)
    for method,path,handler in [('GET','/hr',service.listing),('POST','/hr',service.create),('PUT','/hr/{id}',service.update),('POST','/hr/{id}/hire',service.hire),('GET','/team/{id}/overview',service.overview),('GET','/onboarding',service.onboarding),('POST','/onboarding/progress',service.progress),('POST','/onboarding/assessment',service.assessment)]:
        app.router.add_route(method,'/api/miniapp'+path,handler)
    return service
