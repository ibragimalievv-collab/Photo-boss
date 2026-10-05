"""Exercise persisted sessions through the real auth middleware and disposable DB."""
import json
import time
import unittest
from unittest.mock import patch

import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request, signed

from app import miniapp_sessions as sessions
from app.miniapp_api import MiniApp


class DeviceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.api = self.fixture.service
        self.engine = self.fixture.engine

    async def call(self, raw='', init='', *, uid=1001, custom=True, site='same-origin', path='/me'):
        req = Request('/api/miniapp' + path, uid, 'GET', None, init)
        req.cookies = {sessions.COOKIE: raw} if raw else {}
        if custom:
            req.headers['X-PhotoBoss-Session'] = '1'
        req.headers['Sec-Fetch-Site'] = site
        response = await self.api.middleware(req, self.api.me if path == '/me' else self.api.audit)
        return response

    async def login(self, uid=1001):
        result = await self.call(init=signed(uid))
        assert result.status == 200
        c = result.cookies[sessions.COOKIE]
        assert c['secure'] and c['httponly'] and c['samesite'] == 'Strict'
        assert c['path'] == '/' and c['max-age'] == str(sessions.TTL)
        return c.value

    async def test_reopen_after_three_days_and_server_restart(self):
        raw = await self.login()
        self.api = MiniApp(self.engine, self.fixture.bot, [])
        with patch('app.miniapp_sessions.time.time', return_value=time.time() + 3 * 86400):
            response = await self.call(raw)
        assert response.status == 200
        assert json.loads(response.text)['user']['telegramId'] == 1001
        assert sessions.COOKIE in response.cookies
        with self.engine.inner.connect() as conn:
            stored = conn.execute(text('SELECT key,value FROM settings WHERE key LIKE :p'), {'p': 'miniapp:device:%'}).all()
        assert len(stored) == 1 and raw not in str(stored)

    async def test_expired_tampered_missing_and_cross_site_denied(self):
        raw = await self.login()
        for value in ['', raw + 'x', raw[:-1] + ('A' if raw[-1] != 'A' else 'B')]:
            assert (await self.call(value)).status == 401
        assert (await self.call(raw, custom=False)).status == 401
        assert (await self.call(raw, site='cross-site')).status == 401
        with patch('app.miniapp_sessions.time.time', return_value=time.time() + sessions.TTL + 1):
            assert (await self.call(raw)).status == 401

    async def test_roles_and_disabled_account_checked_each_request(self):
        raw = await self.login()
        with self.engine.inner.begin() as conn:
            conn.execute(text("DELETE FROM user_roles WHERE user_id=1 AND role='OWNER'"))
            conn.execute(text("INSERT INTO user_roles VALUES (1,'PHOTOGRAPHER')"))
        assert (await self.call(raw)).status == 200
        assert (await self.call(raw, path='/audit')).status == 403
        with self.engine.inner.begin() as conn:
            conn.execute(text('UPDATE users SET active=FALSE WHERE id=1'))
        assert (await self.call(raw)).status == 403

    async def test_revocation_and_renewal_throttle(self):
        raw = await self.login()
        assert sessions.COOKIE not in (await self.call(raw)).cookies
        async with self.engine.begin() as conn:
            await sessions.revoke_sessions(conn, 1)
        assert (await self.call(raw)).status == 401

    async def test_fresh_telegram_identity_overrides_existing_cookie(self):
        raw = await self.login()
        response = await self.call(raw, init=signed(1003))
        assert response.status == 200
        assert json.loads(response.text)['user']['telegramId'] == 1003
        assert response.cookies[sessions.COOKIE].value != raw
        # An invalid explicit credential must not silently switch to the cookie account.
        assert (await self.call(raw, init='bad')).status == 401
