"""Real-shoot coaching queue and persisted sales practice."""
import asyncio
import hashlib
import io
import json
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientError, web
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import config
from .miniapp_security import AccessError
from .models import Booking, Photo, Sale, ShootDevelopmentReview, Shooting, utc_now
from .people import positive_id
from .services import development_ai as ai
from .services.insights import parsed


class LimitedImage(io.BytesIO):
    def write(self,data):
        if self.tell()+len(data)>8*1024*1024: raise ValueError('Image exceeds analysis size limit')
        return super().write(data)


async def queue_reviews(session):
    shootings=(await session.execute(select(Shooting,Booking).join(Booking,Shooting.booking_id==Booking.id).where(Shooting.full_upload_completed_at.is_not(None),Booking.photographer_id.is_not(None),select(Sale.id).where(Sale.booking_id==Booking.id).exists()))).all()
    count=0
    for shooting,booking in shootings:
        ids=list((await session.scalars(select(Photo.id).where(Photo.shooting_id==shooting.id).order_by(Photo.id))).all())
        if not ids: continue
        fingerprint=hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        existing=await session.scalar(select(ShootDevelopmentReview.id).where(ShootDevelopmentReview.shooting_id==shooting.id,ShootDevelopmentReview.fingerprint==fingerprint))
        if existing: continue
        session.add(ShootDevelopmentReview(shooting_id=shooting.id,photographer_id=booking.photographer_id,fingerprint=fingerprint,photo_ids=json.dumps(ids)))
        count+=1
    await session.commit()
    return count


async def review_one(factory,bot):
    if not config.openai_api_key: return False
    async with factory() as session:
        row=await session.scalar(select(ShootDevelopmentReview).where(ShootDevelopmentReview.status.in_(['PENDING','RETRY','RUNNING']),ShootDevelopmentReview.attempts<5,
            (ShootDevelopmentReview.claimed_at.is_(None)) | (ShootDevelopmentReview.claimed_at<utc_now()-timedelta(minutes=5))).order_by(ShootDevelopmentReview.id).with_for_update(skip_locked=True).limit(1))
        if not row: return False
        row.status='RUNNING';row.claimed_at=utc_now();row.attempts+=1
        rid=row.id;ids=json.loads(row.photo_ids);batches=json.loads(row.analyzed)
        done={f['photo_id'] for b in batches for f in b['frames']}
        pending=[pid for pid in ids if pid not in done][:6]
        photos=list((await session.scalars(select(Photo).where(Photo.id.in_(pending)))).all()) if pending else []
        await session.commit()
    error=None;response=None
    try:
        if len(ids)>1000: raise ValueError('Полная съёмка превышает лимит 1000 кадров; требуется ручной разбор, выборка не выдаётся за полный анализ.')
        if pending:
            if len(photos)!=len(pending): raise ValueError('Часть исходных фотографий недоступна.')
            images=[]
            for photo in photos:
                data=LimitedImage()
                await bot.download(photo.file_id,destination=data,timeout=20)
                images.append((photo.id,data.getvalue()))
            response=await ai.analyze_batch(images)
        else: response=await ai.summarize_shoot(batches)
        if response['status']!='completed': error='AI недоступен или вернул неполные данные. Анализ не завершён.'
    except (TelegramAPIError, ClientError, TimeoutError, ValueError, OSError, KeyError, TypeError) as exc:
        error=str(exc) if isinstance(exc,ValueError) else 'Не удалось получить фотографии или выполнить анализ. Повторите позднее.'
    async with factory() as session:
        row=await session.get(ShootDevelopmentReview,rid)
        if error:
            row.status='FAILED' if row.attempts>=5 else 'RETRY';row.last_error=error
            # claimed_at gives bounded backoff, including after a process crash.
        elif pending:
            row.analyzed=json.dumps(batches+[response['data']],ensure_ascii=False);row.status='PENDING';row.attempts=0;row.claimed_at=None;row.last_error=None
        else:
            row.result=json.dumps(response['data'],ensure_ascii=False);row.status='COMPLETED';row.completed_at=utc_now();row.last_error=None
        await session.commit()
    return True


class Development:
    def __init__(self,api): self.api=api

    async def listing(self,request):
        a=request['miniapp_actor'];owner='OWNER' in a['roles']
        async with self.api.engine.connect() as conn:
            reviews=await self.api.rows(conn,'SELECT * FROM shoot_development_reviews '+('' if owner else 'WHERE photographer_id=:uid ')+'ORDER BY id DESC LIMIT 50',uid=a['id'])
            sessions=await self.api.rows(conn,'SELECT * FROM sales_training_sessions WHERE user_id=:uid ORDER BY id DESC LIMIT 30',uid=a['id'])
        return web.json_response({'configured':bool(config.openai_api_key),'rules':ai.SALES_RULES,'scenarios':ai.SALES_SCENARIOS,
            'reviews':[{'id':r['id'],'shootingId':r['shooting_id'],'photographerId':r['photographer_id'],'status':r['status'],
                'total':len(json.loads(r['photo_ids'])),'analyzed':sum(len(b['frames']) for b in json.loads(r['analyzed'])),
                'batches':json.loads(r['analyzed']),'result':parsed(r['result']),'error':r['last_error'],'at':str(r['created_at'])} for r in reviews],
            'sessions':[{'id':s['id'],'clientType':s['client_type'],'status':s['status'],'revision':s['revision'],'transcript':json.loads(s['transcript']),'evaluation':parsed(s['evaluation'])} for s in sessions]})

    async def start(self,request):
        a=request['miniapp_actor'];body=await self.api.body(request)
        if set(body)!={'clientType'} or not isinstance(body['clientType'],str) or body['clientType'] not in {s['type'] for s in ai.SALES_SCENARIOS}: raise AccessError('Выберите сценарий.',400)
        if not config.openai_api_key: raise AccessError('AI-тренажёр не подключён. Учебные материалы доступны.',503)
        scenario=next(s for s in ai.SALES_SCENARIOS if s['type']==body['clientType'])
        async with self.api.engine.begin() as conn:
            rows=await self.api.rows(conn,"INSERT INTO sales_training_sessions(user_id,client_type,transcript,status,revision,created_at) VALUES (:uid,:type,:transcript,'ACTIVE',1,:now) RETURNING id",uid=a['id'],type=body['clientType'],transcript=json.dumps([{'role':'client','text':scenario['opening']}],ensure_ascii=False),now=utc_now())
        return web.json_response({'id':rows[0]['id']},status=201)

    async def turn(self,request):
        a=request['miniapp_actor'];sid=positive_id(request.match_info['id']);body=await self.api.body(request)
        if set(body)!={'revision','text','finish'} or type(body['revision']) is not int or type(body['finish']) is not bool or not isinstance(body['text'],str) or len(body['text'])>2000 or (not body['text'].strip() and not body['finish']): raise AccessError('Введите ответ длиной до 2000 символов.',400)
        async with self.api.engine.begin() as conn:
            rows=await self.api.rows(conn,'SELECT * FROM sales_training_sessions WHERE id=:id AND user_id=:uid FOR UPDATE',id=sid,uid=a['id'])
            if not rows: raise AccessError('Тренировка не найдена.',404)
            s=rows[0]
            if s['revision']!=body['revision'] or s['status']!='ACTIVE': raise AccessError('Диалог обновлён. Откройте текущую версию.',409)
            transcript=json.loads(s['transcript'])
            if len(transcript)>=40 and not body['finish']: raise AccessError('Пора завершить тренировку и получить рекомендации.',409)
            if body['text'].strip(): transcript.append({'role':'employee','text':body['text'].strip()})
            response=await ai.roleplay(s['client_type'],transcript,body['finish'])
            if response['status']!='completed': raise AccessError('AI временно недоступен. Ваш ход не потерян в форме; попробуйте снова.',503)
            result=response['data']
            if not isinstance(result.get('reply'),str) or (body['finish'] and (type(result.get('score')) is not int or not 0<=result['score']<=100)): raise AccessError('AI вернул некорректную оценку; повторите попытку.',503)
            transcript.append({'role':'coach' if body['finish'] else 'client','text':result['reply']})
            await conn.execute(text('UPDATE sales_training_sessions SET transcript=:transcript,status=:status,revision=revision+1,evaluation=:evaluation WHERE id=:id'),{'transcript':json.dumps(transcript,ensure_ascii=False),'status':'COMPLETED' if body['finish'] else 'ACTIVE','evaluation':json.dumps(result,ensure_ascii=False) if body['finish'] else None,'id':sid})
        return web.json_response({'ok':True})

    async def retry(self,request):
        a=request['miniapp_actor'];rid=positive_id(request.match_info['id'])
        async with self.api.engine.begin() as conn:
            rows=await self.api.rows(conn,'SELECT photographer_id,status FROM shoot_development_reviews WHERE id=:id',id=rid)
            if not rows: raise AccessError('Разбор не найден.',404)
            if a['id']!=rows[0]['photographer_id'] and 'OWNER' not in a['roles']: raise AccessError('Нет доступа.',403)
            await conn.execute(text("UPDATE shoot_development_reviews SET status='PENDING',attempts=0,claimed_at=NULL WHERE id=:id AND status IN ('FAILED','RETRY')"),{'id':rid})
        return web.json_response({'ok':True})


async def development_loop(engine,bot):
    factory=async_sessionmaker(engine,expire_on_commit=False)
    while True:
        try:
            async with factory() as session: await queue_reviews(session)
            await review_one(factory,bot)
        except asyncio.CancelledError: raise
        except (SQLAlchemyError, OSError, ValueError, TypeError):
            import logging
            logging.getLogger(__name__).warning('Development review cycle unavailable',exc_info=False)
        await asyncio.sleep(15)


def install_development(app,api):
    service=Development(api)
    for method,path,handler in [('GET','/academy/development',service.listing),('POST','/academy/sales-training',service.start),('POST','/academy/sales-training/{id}/turn',service.turn),('POST','/academy/shoot-reviews/{id}/retry',service.retry)]:
        app.router.add_route(method,'/api/miniapp'+path,handler)
    return service
