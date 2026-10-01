"""Recruitment and employee overview, extending existing People and Academy."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .miniapp_security import AccessError, utc_bounds
from .models import utc_now
from .people import People, check_editor, positive_id
from .services.insights import cents, parsed
from .services.onboarding import guides, quiz_for, quiz_kind

HIRING_TERMS = ('Работа в Сочи, Анапе и Крыму. Опыт необязателен: предусмотрено обучение. '
    'Рассматриваем кандидатов из других регионов. Оплата процентная, выплаты еженедельные. '
    'Дорога компенсируется после приезда и минимум месяца работы. Жильё компенсируется частично.')
REGIONS = {'Сочи', 'Анапа', 'Крым'}


def contact_key(value):
    value = value.strip().casefold()
    telegram = re.fullmatch(r'(?:https?://)?(?:t\.me|telegram\.me)/([a-z0-9_]+)/*', value)
    if telegram:
        return '@' + telegram[1]
    phone = re.sub(r'[\s()+.\-]', '', value)
    if phone.isascii() and phone.isdecimal() and 10 <= len(phone) <= 15:
        if len(phone) == 11 and phone.startswith('8'):
            phone = '7' + phone[1:]
        if len(phone) == 10:
            phone = '7' + phone
        return 'phone:' + phone
    return value


def optional_time(body, key):
    value = body.get(key)
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        raise AccessError('Укажите время с часовым поясом.', 400) from None


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
        self.create_lock = asyncio.Lock()

    async def check_recruiter(self,conn,uid):
        rows=await self.api.rows(conn,"SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id WHERE u.id=:uid AND u.active=TRUE AND r.role IN ('OWNER','ADMIN')",uid=uid)
        if not rows: raise AccessError('Ответственным может быть только действующий владелец или администратор.',400)

    async def listing(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        async with self.api.engine.connect() as conn:
            rows=await self.api.rows(conn,'''SELECT c.*,u.name AS responsible_name,
                (SELECT MIN(started_at) FROM shift_check_ins WHERE user_id=c.employee_id AND status='STARTED') AS first_shift_at
                FROM hr_candidates c LEFT JOIN users u ON u.id=COALESCE(c.responsible_id,c.created_by_id)
                ORDER BY c.updated_at DESC,c.id DESC LIMIT 300''')
            recruiters=await self.api.rows(conn,'''SELECT DISTINCT u.id,u.name FROM users u
                JOIN user_roles r ON r.user_id=u.id WHERE u.active=TRUE AND r.role IN ('OWNER','ADMIN') ORDER BY u.name''')
            staff=await self.api.rows(conn,'SELECT id,name,active FROM users ORDER BY name')
            from .services.discipline import compare
            end = self.api.today()-timedelta(days=1)
            discipline = await compare(self.api,conn,end-timedelta(days=6),end)
        return web.json_response({'items':[candidate(r) for r in rows],'stages':STAGES,'employees':[dict(r) for r in staff],'discipline':discipline,
            'recruiters':[dict(r) for r in recruiters], 'terms':HIRING_TERMS,
            'integrations':{'telegram':'not_connected','whatsapp':'not_connected','calls':'not_connected','search':'not_connected','calendar':'not_connected'},
            'externalAutomationEnabled':False})

    async def detail(self,request):
        check_editor(request['miniapp_actor']['roles'])
        cid=positive_id(request.match_info['id'])
        async with self.api.engine.connect() as conn:
            rows=await self.api.rows(conn,"""SELECT c.*,u.name AS responsible_name,
                (SELECT MIN(started_at) FROM shift_check_ins WHERE user_id=c.employee_id AND status='STARTED') AS first_shift_at
                FROM hr_candidates c LEFT JOIN users u ON u.id=COALESCE(c.responsible_id,c.created_by_id) WHERE c.id=:id""",id=cid)
        if not rows: raise AccessError('Кандидат не найден.',404)
        return web.json_response(candidate(rows[0]))

    async def discipline(self, request):
        check_editor(request['miniapp_actor']['roles'])
        start, end = self.api.day(request.query.get('from')), self.api.day(request.query.get('to'))
        if not start <= end <= self.api.today() or (end-start).days > 90:
            raise AccessError('Выберите период не длиннее 91 дня, без будущих дат.',400)
        from .services.discipline import compare
        async with self.api.engine.connect() as conn:
            result = await compare(self.api,conn,start,end)
        return web.json_response(result)

    async def create(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        body=await self.api.body(request)
        if not {'name','contact','source','role'} <= set(body) or set(body)-{'name','contact','source','role','region','responsibleId'} or body.get('role') not in ('PHOTOGRAPHER','MANAGER'):
            raise AccessError('Заполните имя, контакт, источник и рабочую роль.',400)
        values={k:field(body,k,n,required=True) for k,n in [('name',150),('contact',300),('source',150)]}
        region=field(body,'region',50)
        if region and region not in REGIONS: raise AccessError('Выберите Сочи, Анапу или Крым.',400)
        responsible=positive_id(body.get('responsibleId') or actor['id'])
        async with self.create_lock, self.api.engine.begin() as conn:
            await self.people.current_editor(conn,actor['id'])
            await self.check_recruiter(conn,responsible)
            if conn.dialect.name == 'postgresql':
                # Covers different workers and older clients; no destructive merge of legacy duplicates.
                await conn.execute(text('LOCK TABLE hr_candidates IN SHARE ROW EXCLUSIVE MODE'))
            existing=await self.api.rows(conn,'SELECT id,contact FROM hr_candidates')
            key=contact_key(values['contact'])
            for c in existing:
                if contact_key(c['contact']) == key:
                    return web.json_response({'id':c['id'],'alreadyExists':True})
            row=(await self.api.rows(conn,'''INSERT INTO hr_candidates(name,contact,source,role,stage,decision,notes,created_by_id,responsible_id,region,revision,created_at,updated_at)
                VALUES (:name,:contact,:source,:role,'NEW','','[]',:actor,:responsible,:region,1,:now,:now) RETURNING id''',**values,role=body['role'],actor=actor['id'],responsible=responsible,region=region,now=utc_now()))[0]
            await self.api.audit_write(conn,actor,'candidate_created','hr_candidate',row['id'],json.dumps({'before':None,'after':values},ensure_ascii=False))
        return web.json_response({'id':row['id']},status=201)

    async def update(self,request):
        actor=request['miniapp_actor'];check_editor(actor['roles'])
        cid=positive_id(request.match_info['id']);body=await self.api.body(request)
        if not {'revision','stage','interviewAt','decision','note'} <= set(body) or set(body)-{'revision','stage','interviewAt','decision','note','responsibleId','region','reminderAt'} or type(body['revision']) is not int or not isinstance(body['stage'],str) or body['stage'] not in STAGES:
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
            responsible=positive_id(body.get('responsibleId') or old['responsible_id'] or old['created_by_id'])
            await self.check_recruiter(conn,responsible)
            region=field(body,'region',50) if 'region' in body else old['region']
            if region and region not in REGIONS: raise AccessError('Выберите Сочи, Анапу или Крым.',400)
            reminder=optional_time(body,'reminderAt') if 'reminderAt' in body else old['reminder_at']
            stage=body['stage']
            if stage!=old['stage'] and stage not in TRANSITIONS[old['stage']]: raise AccessError('Сначала завершите предыдущий этап. Найм выполняется отдельной кнопкой.',409)
            if stage=='INTERVIEW' and interview is None: raise AccessError('Укажите время интервью.',400)
            if stage in {'OFFER','DOCUMENTS','REJECTED'} and not decision: raise AccessError('Запишите решение.',400)
            notes=json.loads(old['notes'] or '[]')
            if note:
                if len(notes)>=200: raise AccessError('В карточке достигнут предел 200 заметок.',409)
                notes.append({'actorId':actor['id'],'at':str(utc_now()),'text':note})
            new={'stage':stage,'interview_at':str(interview) if interview else None,'decision':decision,'responsible_id':responsible,'region':region,'reminder_at':str(reminder) if reminder else None}
            await conn.execute(text('''UPDATE hr_candidates SET stage=:stage,interview_at=:interview,decision=:decision,notes=:notes,responsible_id=:responsible,region=:region,reminder_at=:reminder,revision=revision+1,updated_at=:now WHERE id=:id'''),
                {'stage':stage,'interview':interview,'decision':decision,'responsible':responsible,'region':region,'reminder':reminder,'notes':json.dumps(notes,ensure_ascii=False),'now':utc_now(),'id':cid})
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
        attempts=await self.api.rows(conn,"SELECT score,result,created_at FROM academy_assessments WHERE user_id=:uid AND kind=:kind ORDER BY id DESC LIMIT 5",uid=uid,kind=quiz_kind(roles))
        practices=await self.api.rows(conn,"SELECT id,status,ai_score FROM training_assignments WHERE user_id=:uid ORDER BY id DESC LIMIT 30",uid=uid)
        from .services.academy import ACADEMY_LESSONS
        lesson_count = len(completed & {lesson.slug for lesson in ACADEMY_LESSONS})
        tasks = []
        def task(key,title,done,route,required=True):
            tasks.append({'key':key,'title':title,'completed':bool(done),'route':route,'required':required})
        task('guides','Пройти инструкции своих ролей',bool(steps) and all(s['slug'] in completed for s in steps),'onboarding')
        task('quiz','Сдать начальный тест: не менее 75%',any(a['score']>=75 for a in attempts),'onboarding')
        if 'PHOTOGRAPHER' in roles:
            task('lessons','Завершить первые четыре урока Академии',lesson_count>=4,'academy')
            task('practice','Сдать первую практику в Академии',any(p['status']=='COMPLETED' for p in practices),'academy')
        if 'MANAGER' in roles:
            bookings = await self.api.rows(conn,'SELECT id FROM bookings WHERE manager_id=:uid LIMIT 1',uid=uid)
            task('booking','Оформить первую рабочую бронь',bookings,'workflow')
        if set(roles) & {'MANAGER','PHOTOGRAPHER'}:
            training = await self.api.rows(conn,"SELECT id FROM sales_training_sessions WHERE user_id=:uid AND status='COMPLETED' LIMIT 1",uid=uid)
            task('sales-practice','Пройти диалог в AI-тренажёре продаж',training,'development',False)
        if 'ADMIN' in roles:
            shifts = await self.api.rows(conn,"SELECT id FROM audit_logs WHERE user_id=:uid AND action IN ('miniapp_shift_created','shift_created') LIMIT 1",uid=uid)
            task('schedule','Назначить первую рабочую смену',shifts,'schedule')
        if 'OWNER' in roles:
            acknowledged = await self.api.rows(conn,'SELECT id FROM notifications WHERE user_id=:uid AND acknowledged_at IS NOT NULL LIMIT 1',uid=uid)
            task('control','Проверить первое событие по данным системы',acknowledged,'insights',False)
        required = [t for t in tasks if t['required']]
        done = sum(t['completed'] for t in required)
        return {'steps':[s|{'completed':s['slug'] in completed} for s in steps],
                'quiz':[{'id':q['id'],'question':q['question'],'choices':q['choices']} for q in quiz_for(roles)],
                'attempts':[{'score':a['score'],'result':parsed(a['result']),'at':str(a['created_at'])} for a in attempts],
                'academyLessons':lesson_count,
                'practices':[dict(p) for p in practices],
                'tasks':[t['title'] for t in tasks], 'taskProgress':tasks,
                'progress':{'completed':done,'total':len(required),'percent':round(done/len(required)*100) if required else 0}}

    async def onboarding(self,request):
        a=request['miniapp_actor']
        async with self.api.engine.connect() as conn: data=await self.onboarding_data(conn,a['id'],a['roles'])
        return web.json_response(data)

    async def progress(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        if set(body)!={'slug'} or not isinstance(body['slug'],str) or body['slug'] not in {s['slug'] for s in guides(a['roles'])}: raise AccessError('Инструкция недоступна вашей роли.',403)
        async with self.api.engine.begin() as conn:
            await conn.execute(text('''INSERT INTO academy_lesson_progress(user_id,topic_slug,completed_at) VALUES (:uid,:slug,:now)
                ON CONFLICT(user_id,topic_slug) DO NOTHING'''),{'uid':a['id'],'slug':body['slug'],'now':utc_now()})
        return web.json_response({'ok':True})

    async def assessment(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        quiz = quiz_for(a['roles'])
        answers=body.get('answers')
        if set(body)!={'answers'} or not isinstance(answers,dict) or set(answers)!={q['id'] for q in quiz} or any(type(answers[q['id']]) is not int or not 0<=answers[q['id']]<len(q['choices']) for q in quiz): raise AccessError('Ответьте на каждый вопрос.',400)
        errors=[{'question':q['question'],'tip':q['tip']} for q in quiz if answers[q['id']]!=q['answer']]
        score=round((len(quiz)-len(errors))/len(quiz)*100)
        result={'errors':errors,'passed':score>=75}
        async with self.api.engine.begin() as conn:
            await conn.execute(text("INSERT INTO academy_assessments(user_id,kind,score,result,created_at) VALUES (:uid,:kind,:score,:result,:now)"),{'uid':a['id'],'kind':quiz_kind(a['roles']),'score':score,'result':json.dumps(result,ensure_ascii=False),'now':utc_now()})
        return web.json_response({'score':score,**result})

    async def overview(self,request):
        a=request['miniapp_actor'];uid=positive_id(request.match_info['id'])
        if uid!=a['id']: check_editor(a['roles'])
        end=self.api.today();start=end-timedelta(days=29)
        async with self.api.engine.connect() as conn:
            profile=await self.people.card(conn,uid)
            # Telegram ID is visible only to the employee and staff administrators.
            data=await self.onboarding_data(conn,uid,profile['roles'])
            from .services.discipline import compare
            report = await compare(self.api,conn,start,end)
            discipline = next((r for r in report['items'] if r['id']==uid), {'confirmed':0,'late':0,'missed':0,'closed':0,'pending':0,'unclosed':0,'trend':[],'missedShifts':[],'repeated':[],'changes':{}})
            discipline |= {'previousFrom':report['previousFrom'],'previousTo':report['previousTo']}
            lo,hi=utc_bounds(start,end,self.api.tz)
            fstart=end if 'ADMIN' in a['roles'] and 'OWNER' not in a['roles'] else start
            flo,fhi=utc_bounds(fstart,end,self.api.tz)
            sales=await self.api.rows(conn,'SELECT id,amount,commission,created_at FROM sales WHERE credited_user_id=:uid AND created_at>=:lo AND created_at<:hi',uid=uid,lo=flo,hi=fhi)
            pay=await self.api.rows(conn,'SELECT kind,amount,note,created_at FROM payroll_entries WHERE user_id=:uid AND created_at>=:lo AND created_at<:hi',uid=uid,lo=flo,hi=fhi)
            payouts=await self.api.rows(conn,"SELECT id,amount,paid_on,note FROM cash_movements WHERE employee_id=:uid AND status='POSTED' AND paid_on>=:start AND paid_on<=:end ORDER BY paid_on DESC",uid=uid,start=fstart,end=end)
            compensation=await self.api.rows(conn,'SELECT role,base_salary,sales_percent FROM compensation WHERE user_id=:uid',uid=uid)
            ratings=await self.api.rows(conn,'SELECT f.rating FROM guest_feedback f JOIN sales s ON s.id=f.sale_id WHERE s.credited_user_id=:uid AND f.submitted_at>=:lo AND f.submitted_at<:hi AND f.rating IS NOT NULL',uid=uid,lo=lo,hi=hi)
            hotels=await self.api.rows(conn,'SELECT h.id,h.name FROM hotels h JOIN hotel_employees he ON he.hotel_id=h.id WHERE he.user_id=:uid',uid=uid)
            bookings = await self.api.rows(conn,'SELECT id,status,shoot_date,shoot_time,hotel_id,room FROM bookings WHERE (manager_id=:uid OR photographer_id=:uid) AND shoot_date>=:start AND shoot_date<=:end ORDER BY shoot_date DESC,id DESC',uid=uid,start=start,end=end)
            audits = await self.api.rows(conn,'SELECT id,action,entity,entity_id,created_at FROM audit_logs WHERE user_id=:uid AND created_at>=:lo AND created_at<:hi ORDER BY id DESC LIMIT 100',uid=uid,lo=flo,hi=fhi)
            reviews = await self.api.rows(conn,'SELECT id,shooting_id,status,result,created_at FROM shoot_development_reviews WHERE photographer_id=:uid ORDER BY id DESC LIMIT 30',uid=uid)
            training = await self.api.rows(conn,'SELECT id,client_type,status,evaluation,created_at FROM sales_training_sessions WHERE user_id=:uid ORDER BY id DESC LIMIT 30',uid=uid)
        detailed = uid==a['id'] or 'OWNER' in a['roles']
        return web.json_response({'profile':profile,'hotels':[dict(h) for h in hotels],'from':str(start),'to':str(end),
            'feedback':{'count':len(ratings),'average':round(sum(r['rating'] for r in ratings)/len(ratings),2) if ratings else None},
            'discipline':discipline,
            'finance':{'paidOut':sum(cents(r['amount']) for r in payouts),'payouts':[{'id':r['id'],'amount':cents(r['amount']),'date':str(r['paid_on']),'note':r['note']} for r in payouts], 'compensation':[{'role':r['role'],'baseSalary':cents(r['base_salary']),'salesPercent':r['sales_percent']} for r in compensation], 'from':str(fstart),'to':str(end),'revenue':sum(cents(s['amount']) for s in sales),'sales':len(sales),'commission':sum(cents(s['commission']) for s in sales),'adjustments':sum(cents(p['amount']) for p in pay),'entries':[{'kind':p['kind'],'amount':cents(p['amount']),'note':p['note'],'at':str(p['created_at'])} for p in pay],
                'saleHistory':[{'id':s['id'],'amount':cents(s['amount']),'commission':cents(s['commission']),'at':str(s['created_at'])} for s in sales]},
            'bookings':[dict(b)|{'shoot_date':str(b['shoot_date']),'shoot_time':str(b['shoot_time'])} for b in bookings],
            'history':[dict(r)|{'created_at':str(r['created_at'])} for r in audits],
            'reviews':[{'id':r['id'],'shootingId':r['shooting_id'],'status':r['status'],'result':parsed(r['result']) if detailed else {},'at':str(r['created_at'])} for r in reviews],
            'salesTraining':[{'id':r['id'],'clientType':r['client_type'],'status':r['status'],'evaluation':parsed(r['evaluation']) if detailed else {},'at':str(r['created_at'])} for r in training],
            'onboarding':data})


def install_team(app,api):
    service=Team(api)
    for method,path,handler in [('GET','/discipline',service.discipline),('GET','/hr',service.listing),('POST','/hr',service.create),('GET','/hr/{id}',service.detail),('PUT','/hr/{id}',service.update),('POST','/hr/{id}/hire',service.hire),('GET','/team/{id}/overview',service.overview),('GET','/onboarding',service.onboarding),('POST','/onboarding/progress',service.progress),('POST','/onboarding/assessment',service.assessment)]:
        app.router.add_route(method,'/api/miniapp'+path,handler)
    return service
