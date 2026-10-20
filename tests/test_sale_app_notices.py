import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_insights import fixture

from app.miniapp_security import AccessError
from app.models import Booking, Hotel, HotelEmployee, Notification, Sale, User, UserRole
from app.push_notifications import AppPush, eligible, queue_sale_created


def test_completed_sale_notifies_related_people_once_and_rechecks_hotel_scope():
    async def run():
        engine,factory,_=await fixture()
        api=SimpleNamespace(engine=engine,body=AsyncMock(side_effect=lambda r:r['body']))
        service=AppPush(api)
        try:
            async with factory() as s:
                s.add_all([UserRole(user_id=2,role='PHOTOGRAPHER'),
                    User(id=3,tg_id=3,name='Scoped admin'),UserRole(user_id=3,role='ADMIN'),HotelEmployee(user_id=3,hotel_id=1),
                    User(id=4,tg_id=4,name='Other admin'),UserRole(user_id=4,role='ADMIN'),Hotel(id=2,name='Other'),HotelEmployee(user_id=4,hotel_id=2),
                    User(id=5,tg_id=5,name='Unrelated photographer'),UserRole(user_id=5,role='PHOTOGRAPHER')])
                await s.flush()
                sale,booking=await s.get(Sale,1),await s.get(Booking,1)
                await queue_sale_created(s,sale,booking)
                await queue_sale_created(s,sale,booking)
                await s.commit()
            async with factory() as s:
                notices=list(await s.scalars(select(Notification).where(Notification.kind=='sale_created')))
                assert {n.user_id for n in notices}=={1,2,3} and len(notices)==3
                scoped=next(n for n in notices if n.user_id==3)
                sub=SimpleNamespace(active=True,user_id=3)
                assert await eligible(s,scoped,sub)
                membership=await s.scalar(select(HotelEmployee).where(HotelEmployee.user_id==3))
                await s.delete(membership);await s.commit()
                assert not await eligible(s,scoped,sub)
            listed=json.loads((await service.status({'miniapp_actor':{'id':3}})).text)
            assert listed['items']==[]
            own=json.loads((await service.status({'miniapp_actor':{'id':1}})).text)['items']
            assert own[0]['title']=='Photo Boss · Оформлена продажа'
            await service.read({'miniapp_actor':{'id':1},'body':{'id':own[0]['id']}})
            again=json.loads((await service.status({'miniapp_actor':{'id':1}})).text)['items']
            assert again[0]['read']
            with pytest.raises(AccessError):
                await service.read({'miniapp_actor':{'id':2},'body':{'id':own[0]['id']}})
        finally:await engine.dispose()
    asyncio.run(run())
