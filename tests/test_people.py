"""Staff mutations run against a disposable DB and the real auth middleware."""
import json
import unittest
from urllib.parse import urlsplit

import pytest
import test_miniapp_release as baseline
from sqlalchemy import text

from app.miniapp_security import AccessError
from app.people import GENERAL_RULES, People, check_editor, positive_id, staff_payload


def payload(**changes):
    return {"name": "New employee", "telegramId": "2001", "roles": ["PHOTOGRAPHER"], "hotelIds": [1]} | changes


@pytest.mark.parametrize("bad", [True, 0, -1, 1.2, [], {}, "-1", "1e3", "１", "", 2**52])
def test_id_validation(bad):
    with pytest.raises(AccessError):
        positive_id(bad, maximum=2**52-1)


@pytest.mark.parametrize("changes", [{"name": ""}, {"name": "x\ny"}, {"name": "a"*151}, {"roles": []},
    {"roles": ["OWNER"]}, {"roles": ["PHOTOGRAPHER","PHOTOGRAPHER"]}, {"roles": [[]]},
    {"hotelIds": [True]}, {"hotelIds": [1,1]}, {"extra": "role_override"}, {"telegramId": True}])
def test_invalid_payload(changes):
    with pytest.raises(AccessError):
        staff_payload(payload(**changes))


def test_policy_and_plain_document():
    for roles,target,own in [(["MANAGER"],[],False),(["ADMIN"],["ADMIN"],False),
                             (["OWNER"],["OWNER"],False),(["OWNER"],[],True)]:
        with pytest.raises(AccessError):
            check_editor(roles,target,self_edit=own)
    check_editor(["OWNER"],["ADMIN"])
    assert "включая сообщения между двумя сотрудниками" in GENERAL_RULES
    assert "не является договором оказания услуг" in GENERAL_RULES


class StaffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await baseline.MiniAppTests.asyncSetUp(self)
        self.people = People(self.service)
        with self.engine.inner.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
            conn.execute(text("ALTER TABLE users ADD COLUMN terminated_at DATETIME"))
            conn.execute(text("ALTER TABLE shootings ADD COLUMN status TEXT"))
            conn.execute(text("CREATE UNIQUE INDEX unique_tg_id ON users(tg_id)"))

    async def asyncTearDown(self):
        await baseline.MiniAppTests.asyncTearDown(self)

    async def call(self,path='/people',uid=1001,method='GET',body=None,token=None):
        req=baseline.Request('/api/miniapp'+path,uid,method,body,token)
        route=urlsplit(path).path
        fn=self.people.documents if route=='/documents' else self.people.listing
        if method=='POST':
            if route.endswith('/fire'):
                fn=self.people.fire
                req.match_info={'id':route.split('/')[-2]}
            elif route.endswith('/restore'):
                fn=self.people.restore
                req.match_info={'id':route.split('/')[-2]}
            else:
                fn=self.people.create
        if method=='PUT':
            if route.endswith('/screen-capture'):
                fn=self.people.set_screen_capture
                req.match_info={'id':route.split('/')[-2]}
            else:
                fn=self.people.update
                req.match_info={'id':route.rsplit('/',1)[1]}
        response=await self.service.middleware(req,fn)
        return response.status,json.loads(response.text)

    async def edit(self,uid=3,actor=1001,**changes):
        _,data=await self.call(uid=actor)
        card=next(x for x in data['items'] if x['id']==uid)
        body={k:card[k] for k in ('name','roles','hotelIds','active','revision')} | changes
        return await self.call(f'/people/{uid}',uid=actor,method='PUT',body=body)

    async def test_role_matrix(self):
        for uid in (1003,1004,1005,1006,1007):
            assert (await self.call(uid=uid))[0]==403
            assert (await self.call(uid=uid,method='POST',body=payload()))[0]==403
        assert (await self.call(token=''))[0]==401
        assert (await self.call(uid=1002))[0]==200
        assert (await self.call('/people?role=OWNER',uid=1003))[0]==400

    async def test_create_duplicate_and_audit(self):
        status,res=await self.call(method='POST',body=payload())
        assert status==201 and res['employee']['roles']==['PHOTOGRAPHER']
        assert (await self.call(method='POST',body=payload(name='Changed')))[0]==409
        with self.engine.inner.connect() as c:
            assert c.execute(text("SELECT name FROM users WHERE tg_id=2001")).scalar()=='New employee'
            assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='employee_created'")).scalar()==1
            assert c.execute(text("SELECT COUNT(*) FROM hotel_employees WHERE user_id=:id"),{'id':res['employee']['id']}).scalar()==1

    async def test_protected_roles_and_self(self):
        assert (await self.edit(uid=2,actor=1002,name='Self'))[0]==403
        # Valid assignable roles reach the owner-account guard rather than failing payload validation.
        assert (await self.edit(uid=1,name='Owner',roles=['PHOTOGRAPHER']))[0]==403
        assert (await self.edit(uid=1,actor=1002,name='Owner',roles=['PHOTOGRAPHER']))[0]==403
        assert (await self.edit(roles=['OWNER']))[0]==400
        assert (await self.edit(actor=1002,roles=['ADMIN']))[0]==403
        assert (await self.call(uid=1002,method='POST',body=payload(roles=['ADMIN'])))[0]==403
        with self.engine.inner.connect() as c:
            assert c.execute(text("SELECT name FROM users WHERE id=1")).scalar()=='User 1'
            assert c.execute(text("SELECT role FROM user_roles WHERE user_id=1")).scalar()=='OWNER'
            assert c.execute(text("SELECT role FROM user_roles WHERE user_id=3")).scalar()=='PHOTOGRAPHER'
        assert (await self.edit(uid=2,name='Admin updated'))[0]==200

    async def test_update_and_revision(self):
        assert (await self.edit(name='Updated',roles=['MANAGER','PHOTOGRAPHER'],hotelIds=[1,2]))[0]==200
        assert (await self.edit(revision='0'*64))[0]==409
        assert (await self.edit(hotelIds=[99]))[0]==409
        with self.engine.inner.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='miniapp_employee_updated'")).scalar()==1
            detail=c.execute(text("SELECT details FROM audit_logs WHERE action='miniapp_employee_updated'")).scalar()
            assert json.loads(detail)['before']['name']=='User 3'

    async def test_fire_moves_employee_to_archive_without_deleting_history(self):
        assert (await self.call('/people/3/fire',method='POST',body={}))[0]==200
        assert (await self.call('/documents',uid=1003))[0]==403
        _,active=await self.call()
        assert all(item['id']!=3 for item in active['items'])
        _,archived=await self.call('/people?archived=1')
        fired=next(item for item in archived['items'] if item['id']==3)
        assert fired['active'] is False and fired['terminatedAt']
        with self.engine.inner.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM sales WHERE credited_user_id=3")).scalar()==1
        assert (await self.call('/people/3/restore',method='POST',body={}))[0]==200
        _,active=await self.call()
        assert any(item['id']==3 for item in active['items'])

    async def test_owner_controls_screen_capture_and_sees_last_login(self):
        _, listing = await self.call()
        owner = next(x for x in listing['items'] if x['id'] == 1)
        photographer = next(x for x in listing['items'] if x['id'] == 3)
        assert listing['canManageScreenCapture'] is True
        assert owner['screenCaptureAllowed'] is True
        assert photographer['screenCaptureAllowed'] is False
        assert (await self.call('/people/3/screen-capture', uid=1002, method='PUT', body={'allowed': True}))[0] == 403
        status, changed = await self.call('/people/3/screen-capture', method='PUT', body={'allowed': True})
        assert status == 200 and changed['employee']['screenCaptureAllowed'] is True

        request = baseline.Request('/api/miniapp/session', 1003, 'POST', {}, None)
        response = await self.service.middleware(request, self.service.session_open)
        assert response.status == 200
        _, listing = await self.call()
        photographer = next(x for x in listing['items'] if x['id'] == 3)
        assert photographer['lastLoginAt']
        with self.engine.inner.connect() as conn:
            assert conn.execute(text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='miniapp_screen_capture_changed'"
            )).scalar() == 1

    async def test_latest_role_rechecked(self):
        with self.engine.inner.begin() as c:
            c.execute(text("UPDATE user_roles SET role='MANAGER' WHERE user_id=2"))
        assert (await self.call(uid=1002,method='POST',body=payload()))[0]==403

    async def test_documents_are_not_consent_or_signed_contract(self):
        status,res=await self.call('/documents',uid=1003)
        assert status==200 and res['status']=='draft' and res['canSign'] is False
        assert 'Не подготовлен' in res['servicesContract']
        with self.engine.inner.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()==1
