"""Password binding, recovery, revocation and role isolation through real middleware."""
import json
import time
import unittest
from unittest.mock import patch

import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request, signed

from app import miniapp_sessions as sessions
from app.browser_login import BrowserLogin
from app.password_login import PasswordLogin, password_hash, password_matches

ORIGIN = 'https://photo-boss.onrender.com'
PASSWORD = 'personal-test-password-42'


class PasswordLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api = self.fixture.service
        self.engine = self.fixture.engine
        self.service = PasswordLogin(self.api, ORIGIN)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def request(self, path, body=None, *, uid=1001, cookie='', init='', headers=None):
        req = Request(path, uid, 'POST' if path.startswith('/auth/') else 'PUT', body, init)
        req.cookies = {sessions.COOKIE: cookie} if cookie else {}
        req.headers.update({'X-PhotoBoss-Session': '1', 'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin'})
        req.headers.update(headers or {})
        req.remote = '127.0.0.1'
        return req

    async def setup(self, login='owner', *, uid=1001, cookie='', init=None, password=PASSWORD, current=None):
        body = {'login': login, 'password': password}
        if current is not None:
            body['currentPassword'] = current
        req = self.request('/api/miniapp/account/password', body, uid=uid, cookie=cookie,
                           init=signed(uid) if init is None else init)
        return await self.api.middleware(req, self.service.setup)

    async def login(self, login='owner', password=PASSWORD, **kwargs):
        req = self.request('/auth/password/login', {'login': login, 'password': password}, **kwargs)
        return await self.service.endpoint(req)

    async def me(self, raw, path='/me'):
        req = self.request('/api/miniapp' + path, cookie=raw)
        return await self.api.middleware(req, self.api.me if path == '/me' else self.api.audit)

    async def test_once_telegram_then_password_and_restart_preserve_role(self):
        response = await self.setup('Photographer', uid=1003)
        assert response.status == 200, response.text
        self.service = PasswordLogin(self.api, ORIGIN)
        login = await self.login('PHOTOGRAPHER')
        assert login.status == 200, login.text
        raw = login.cookies[sessions.COOKIE].value
        assert login.cookies[sessions.COOKIE]['httponly'] and login.cookies[sessions.COOKIE]['secure']
        me = json.loads((await self.me(raw)).text)
        assert me['user']['roles'] == ['PHOTOGRAPHER']
        assert me['passwordLogin'] == 'photographer' and me['passwordConfigured']
        assert (await self.me(raw, '/audit')).status == 403
        with self.engine.inner.connect() as conn:
            saved = str(conn.execute(text('SELECT * FROM settings')).all())
            audit = str(conn.execute(text('SELECT * FROM audit_logs')).all())
        assert PASSWORD not in saved and raw not in saved and 'scrypt-v1' not in audit

    async def test_setup_needs_fresh_telegram_or_existing_password(self):
        async with self.engine.begin() as conn:
            raw = await sessions.issue(conn, {'id': 1, 'tg_id': 1001}, '', int(time.time()))
        assert (await self.setup(cookie=raw, init='')).status == 403
        assert (await self.setup(init=signed(issued=int(time.time())-601))).status == 403
        assert (await self.setup()).status == 200
        raw = (await self.login()).cookies[sessions.COOKIE].value
        assert (await self.setup('owner2', cookie=raw, init='', current=PASSWORD)).status == 200
        assert (await self.login('owner')).status == 401
        assert (await self.login('owner2')).status == 200

    async def test_browser_approval_is_sufficient_but_proof_expires(self):
        # This is the proof marker issued by the existing browser approval endpoint.
        async with self.engine.begin() as conn:
            raw = await sessions.issue(conn, {'id': 1, 'tg_id': 1001}, '', int(time.time()),
                                       telegram_verified_at=int(time.time()))
        with patch('app.password_login.time.time', return_value=time.time()+601):
            assert (await self.setup(cookie=raw, init='')).status == 403
        assert (await self.setup(cookie=raw, init='')).status == 200

    async def test_bad_password_missing_account_and_disabled_access(self):
        assert (await self.setup()).status == 200
        for login, password in [('owner', 'wrong-password-123'), ('missing', PASSWORD)]:
            response = await self.login(login, password)
            assert response.status == 401 and sessions.COOKIE not in response.cookies
        with self.engine.inner.begin() as conn:
            conn.execute(text('UPDATE users SET active=FALSE WHERE id=1'))
        assert (await self.login()).status == 403

    async def test_uniqueness_and_current_password_cannot_claim_other_account(self):
        assert (await self.setup()).status == 200
        assert (await self.setup('OWNER', uid=1003)).status == 409
        assert (await self.setup('staff', uid=1003)).status == 200
        staff = (await self.login('staff')).cookies[sessions.COOKIE].value
        body = {'login': 'new', 'password': PASSWORD, 'userId': 1}
        req = self.request('/api/miniapp/account/password', body, cookie=staff)
        assert (await self.api.middleware(req, self.service.setup)).status == 400
        assert (await self.setup('new', cookie=staff, init='', uid=1003, current='invalid-password-99')).status == 403

    async def test_logout_revokes_one_device_and_racing_refresh_cannot_restore_it(self):
        assert (await self.setup()).status == 200
        first = (await self.login()).cookies[sessions.COOKIE].value
        second = (await self.login()).cookies[sessions.COOKIE].value
        response = await self.service.endpoint(self.request('/auth/password/logout', cookie=first))
        assert response.status == 200 and response.cookies[sessions.COOKIE]['max-age'] == '0'
        assert (await self.me(first)).status == 401
        signed_req = self.request('/api/miniapp/me', cookie=first, init=signed())
        raced = await self.api.middleware(signed_req, self.api.me)
        assert sessions.COOKIE not in raced.cookies
        assert (await self.me(second)).status == 200
        async with self.engine.begin() as conn:
            assert await sessions.issue(conn, {'id': 1, 'tg_id': 1001}, first, int(time.time())) is None
        assert (await self.me(first)).status == 401

    async def test_switch_account_and_reset_revoke_previous_sessions(self):
        assert (await self.setup()).status == 200
        assert (await self.setup('staff', uid=1003)).status == 200
        old = (await self.login()).cookies[sessions.COOKIE].value
        new = (await self.login('staff', cookie=old)).cookies[sessions.COOKIE].value
        assert (await self.me(old)).status == 401
        assert json.loads((await self.me(new)).text)['user']['telegramId'] == 1003
        await self.setup('staff', uid=1003, password='changed-test-password-123')
        assert (await self.me(new)).status == 401
        assert (await self.login('staff')).status == 401
        assert (await self.login('staff', 'changed-test-password-123')).status == 200

    async def test_csrf_and_rate_limit(self):
        assert (await self.setup()).status == 200
        for headers in [{'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'X-PhotoBoss-Session': ''}]:
            assert (await self.login(headers=headers)).status == 403
            req = self.request('/auth/password/logout', headers=headers)
            assert (await self.service.endpoint(req)).status == 403
        for _ in range(10):
            assert (await self.login(password='invalid-test-password')).status == 401
        self.service = PasswordLogin(self.api, ORIGIN)
        assert (await self.login()).status == 429

    async def test_validation_and_no_telegram_approval_reuse_after_logout(self):
        for login, password in [('xx', PASSWORD), ('owner', 'short'), ('owner', 'x'*129)]:
            assert (await self.setup(login, password=password)).status == 400
        browser = BrowserLogin(self.engine, self.fixture.bot, ORIGIN)
        response = await browser.endpoint(self.request('/auth/browser/start'))
        from app.browser_login import COOKIE, PREFIX
        approval = response.cookies[COOKIE].value
        req = self.request('/auth/password/logout')
        req.cookies[COOKIE] = approval
        assert (await self.service.endpoint(req)).status == 200
        with self.engine.inner.connect() as conn:
            assert conn.execute(text('SELECT value FROM settings WHERE key=:key'),
                                {'key': PREFIX+approval.split('.')[0]}).first() is None


def test_password_hash_is_salted_and_exact():
    one, two = password_hash(PASSWORD), password_hash(PASSWORD)
    assert one != two and password_matches(PASSWORD, one)
    assert not password_matches(PASSWORD.upper(), one)
    assert not password_matches(PASSWORD, 'corrupt')
