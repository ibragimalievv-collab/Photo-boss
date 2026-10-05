"""Telegram approval must be bound to the initiating browser, expire and preserve roles."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import test_miniapp_release as fixtures
from sqlalchemy import text
from test_miniapp_release import Request

from app import miniapp_sessions as sessions
from app.browser_login import COOKIE, PREFIX, TTL, BrowserLogin, handle_start
from app.miniapp_security import AccessError

ORIGIN = 'https://photo-boss.onrender.com'


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.MiniAppTests()
        await self.fixture.asyncSetUp()
        self.engine = self.fixture.engine
        self.service = BrowserLogin(self.engine, self.fixture.bot, ORIGIN)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def request(self, path='start', cookie='', **headers):
        req = Request('/auth/browser/' + path, 0, 'POST', None, '')
        req.cookies = {COOKIE: cookie} if cookie else {}
        req.headers = {'X-PhotoBoss-Session': '1', 'Origin': ORIGIN, 'Sec-Fetch-Site': 'same-origin', **headers}
        req.remote = '127.0.0.1'
        return req

    async def start(self):
        response = await self.service.endpoint(self.request())
        assert response.status == 200, response.text
        data = json.loads(response.text)
        cookie = response.cookies[COOKIE].value
        identifier = parse_qs(urlsplit(data['telegramUrl']).query)['start'][0][4:]
        assert identifier == cookie.split('.')[0]
        assert len(data['code']) == 6 and data['code'].isdigit()
        assert response.cookies[COOKIE]['secure'] and response.cookies[COOKIE]['httponly']
        assert response.cookies[COOKIE]['samesite'] == 'Strict'
        assert cookie.split('.')[1] not in response.text
        return identifier, cookie, data

    def message(self, uid=1001, *, private=True):
        return SimpleNamespace(chat=SimpleNamespace(type='private' if private else 'group'),
                               from_user=SimpleNamespace(id=uid, is_bot=False), answer=AsyncMock())

    def callback(self, uid=1001):
        return SimpleNamespace(message=self.message(uid), from_user=SimpleNamespace(id=uid, is_bot=False),
                               answer=AsyncMock())

    async def approve(self, identifier, uid=1001):
        message = self.message(uid)
        await self.service.prompt(message, identifier)
        assert 'Код на сайте:' in message.answer.call_args.args[0]
        assert len(message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data) <= 64
        await self.service.approve(self.callback(uid), identifier, True)

    async def me(self, cookie, path='/me'):
        req = Request('/api/miniapp' + path, 0, 'GET', None, '')
        req.cookies = {sessions.COOKIE: cookie}
        req.headers = {'X-PhotoBoss-Session': '1', 'Sec-Fetch-Site': 'same-origin'}
        return await self.fixture.service.middleware(req, self.fixture.service.me if path == '/me' else self.fixture.service.audit)

    async def test_full_login_reopen_session_and_one_time_consumption(self):
        identifier, cookie, _ = await self.start()
        pending = await self.service.endpoint(self.request('poll', cookie))
        assert json.loads(pending.text)['status'] == 'pending'
        assert sessions.COOKIE not in pending.cookies
        await self.approve(identifier)
        response = await self.service.endpoint(self.request('poll', cookie))
        assert response.status == 200
        raw = response.cookies[sessions.COOKIE].value
        assert response.cookies[sessions.COOKIE]['max-age'] == str(sessions.TTL)
        assert (await self.me(raw)).status == 200
        with patch('app.miniapp_sessions.time.time', return_value=time.time() + 3 * 86400):
            assert (await self.me(raw)).status == 200
        assert (await self.service.endpoint(self.request('poll', cookie))).status == 401
        with self.engine.inner.connect() as conn:
            assert not conn.execute(text('SELECT value FROM settings WHERE key=:key'), {'key': PREFIX + identifier}).first()
            saved = str(conn.execute(text('SELECT value FROM settings')).all())
            assert raw not in saved and cookie.split('.')[1] not in saved

    async def test_wrong_browser_and_missing_cookie_never_receive_session(self):
        identifier, cookie, _ = await self.start()
        await self.approve(identifier)
        wrong = identifier + '.' + 'A' * 43
        for value in ['', wrong, cookie + 'x']:
            response = await self.service.endpoint(self.request('poll', value))
            assert response.status == 401 and sessions.COOKIE not in response.cookies
        assert (await self.service.endpoint(self.request('poll', cookie))).status == 200

    async def test_csrf_origin_and_header_checks(self):
        for headers in [{'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': ''},
                        {'Sec-Fetch-Site': 'cross-site'}, {'X-PhotoBoss-Session': ''}]:
            assert (await self.service.endpoint(self.request(**headers))).status == 403
            assert (await self.service.endpoint(self.request('poll', **headers))).status == 403

    async def test_approval_requires_prompt_and_same_telegram_account(self):
        identifier, cookie, _ = await self.start()
        with self.assertRaises(AccessError):
            await self.service.approve(self.callback(), identifier, True)
        await self.service.prompt(self.message(), identifier)
        with self.assertRaises(AccessError):
            await self.service.approve(self.callback(1002), identifier, True)
        assert json.loads((await self.service.endpoint(self.request('poll', cookie))).text)['status'] == 'pending'

    async def test_disabled_unassigned_and_bot_accounts_denied(self):
        for uid in [1006, 1007, 12345]:
            identifier, cookie, _ = await self.start()
            with self.assertRaises(AccessError):
                await self.service.prompt(self.message(uid), identifier)
            assert sessions.COOKIE not in (await self.service.endpoint(self.request('poll', cookie))).cookies
        identifier, cookie, _ = await self.start()
        await self.approve(identifier)
        with self.engine.inner.begin() as conn:
            conn.execute(text('UPDATE users SET active=FALSE WHERE id=1'))
        assert (await self.service.endpoint(self.request('poll', cookie))).status == 403

    async def test_roles_preserved_and_checked_after_browser_login(self):
        identifier, cookie, _ = await self.start()
        await self.approve(identifier, 1003)
        response = await self.service.endpoint(self.request('poll', cookie))
        raw = response.cookies[sessions.COOKIE].value
        me = await self.me(raw)
        assert json.loads(me.text)['user']['roles'] == ['PHOTOGRAPHER']
        assert (await self.me(raw, '/audit')).status == 403
        async with self.engine.begin() as conn:
            await sessions.revoke_sessions(conn, 3)
        assert (await self.me(raw)).status == 401

    async def test_cancellation_expiry_and_duplicate_approval(self):
        identifier, cookie, _ = await self.start()
        await self.service.prompt(self.message(), identifier)
        await self.service.approve(self.callback(), identifier, False)
        assert (await self.service.endpoint(self.request('poll', cookie))).status == 403
        with self.assertRaises(AccessError):
            await self.service.approve(self.callback(), identifier, True)
        identifier, cookie, _ = await self.start()
        with patch('app.browser_login.time.time', return_value=time.time() + TTL + 1):
            assert (await self.service.endpoint(self.request('poll', cookie))).status == 401
            with self.assertRaises(AccessError):
                await self.service.prompt(self.message(), identifier)

    async def test_database_failure_does_not_consume_approval(self):
        identifier, cookie, _ = await self.start()
        await self.approve(identifier)
        with patch('app.browser_login.sessions.issue', side_effect=RuntimeError('temporary test failure')):
            assert (await self.service.endpoint(self.request('poll', cookie))).status == 503
        assert (await self.service.endpoint(self.request('poll', cookie))).status == 200

    async def test_rate_limit_is_persistent_and_recovers(self):
        for _ in range(30):
            assert (await self.service.endpoint(self.request())).status == 200
        self.service = BrowserLogin(self.engine, self.fixture.bot, ORIGIN)
        assert (await self.service.endpoint(self.request())).status == 429
        assert (await self.service.endpoint(self.request())).status == 429
        with patch('app.browser_login.time.time', return_value=time.time() + TTL + 1):
            assert (await self.service.endpoint(self.request())).status == 200

    async def test_telegram_start_intercepts_only_browser_requests(self):
        msg = self.message()
        msg.text = '/start'
        assert not await handle_start(msg)
        identifier, _, _ = await self.start()
        msg.text = '/start web_' + identifier
        msg.bot = self.fixture.bot
        with patch('app.browser_login.service_for_bot', return_value=self.service):
            assert await handle_start(msg)
        assert msg.answer.await_count == 1

    async def test_login_module_is_served_by_real_static_allowlist(self):
        req = Request('/app/js/browser-login.js', 0, 'GET', None, '')
        req.match_info = {'asset': 'js/browser-login.js'}
        response = await self.fixture.service.middleware(req, self.fixture.service.static_file)
        assert response.status == 200
        assert response.headers['Cache-Control'] == 'no-store'

    async def test_lost_json_response_recovers_only_with_valid_device_cookie(self):
        identifier, cookie, _ = await self.start()
        await self.approve(identifier)
        response = await self.service.endpoint(self.request('poll', cookie))
        raw = response.cookies[sessions.COOKIE].value
        for previous in [cookie, '']:
            retry = self.request('poll', previous)
            retry.cookies[sessions.COOKIE] = raw
            assert json.loads((await self.service.endpoint(retry)).text)['status'] == 'authenticated'
        async with self.engine.begin() as conn:
            await sessions.revoke_sessions(conn, 1)
        assert (await self.service.endpoint(retry)).status == 401
