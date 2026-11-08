"""Optional, private WebSocket/PCM transport for owned Academy voice leases.

The one-use connection ticket travels as a WebSocket subprotocol, never a URL.
It carries no role or user ID. Upstream credentials never reach the browser.
"""
import asyncio
import base64
from contextlib import suppress
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from .miniapp_security import AccessError, STAFF_ROLES

RATE = 44100
MAX_FRAME = 32768


def settings_for(settings, config):
    folder = getattr(config, 'yandex_cloud_folder_id', '')
    key = getattr(config, 'yandex_cloud_api_key', '')
    model = getattr(config, 'yandex_realtime_model', 'speech-realtime-deepseek-v4-flash')
    turn_mode = getattr(config, 'yandex_realtime_turn_mode', 'server_vad')
    if (not key or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', folder)
            or not re.fullmatch(r'speech-realtime-[a-z0-9-]{1,90}(?:/latest)?', model)
            or turn_mode not in {'server_vad', 'application'}):
        raise AccessError('Голосовой ИИ не подключён. Обратитесь к владельцу.', 503)
    output = json.loads(json.dumps(settings))
    output.pop('model', None)
    output['instructions'] += (
        ' Не называй длительность съёмки, конкретную локацию, свободное время или '
        'подтверждённую цену, если их нет в сохранённых данных. Сначала уточни такие '
        'данные. Пример фразы для гостя также не должен содержать выдуманных условий.')
    output['audio'] = {
        'input': {'format': {'type': 'audio/pcm', 'rate': RATE}, 'languages': ['ru-RU'],
                  'turn_detection': {'type': 'server_vad', 'threshold': 0.5, 'silence_duration_ms': 400}},
        'output': {'format': {'type': 'audio/pcm', 'rate': RATE}, 'voice': 'dasha'},
    }
    if turn_mode == 'application':
        output['audio']['input']['turn_detection'] = None
    if model == 'speech-realtime-deepseek-v4-flash':
        # Speech must begin with an answer, not spend its small audible-output
        # allowance on hidden reasoning. This field is part of Yandex's API.
        output['reasoning'] = {'effort': 'none'}
    # Retain the complete system/history snapshot and its app token limit.
    return output, ('wss://ai.api.cloud.yandex.net/v1/realtime/openai?'
                    + urlencode({'model': 'gpt://' + folder + '/' + model})), model


def origin_guard(request):
    from .launch_policy import app_url
    origin = request.headers.get('Origin', '')
    canonical = app_url().split('/app/', 1)[0]
    allowed = {canonical}
    pair = {'https://photo-boss.ru', 'https://217.198.13.9:18443'}
    if canonical in pair:
        allowed |= pair
    if (origin not in allowed or urlsplit(origin).netloc != request.host
            or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        raise AccessError('Откройте голосовой разговор в Фотобоссе.', 403)


class YandexRealtime:
    def __init__(self, parent):
        self.parent, self.api = parent, parent.api
        self.pending, self.active = {}, {}

    def prepare(self, actor, current, lease, settings, config, request=None):
        snapshot, url, model = settings_for(settings, config)
        ticket = secrets.token_urlsafe(32)
        snapshot_actor = {'id': actor['id'], 'roles': sorted(actor['roles'])}
        if request is not None and request.get('miniapp_auth_method'):
            from . import miniapp_sessions
            snapshot_actor['authCookie'] = miniapp_sessions.cookie(request)
        self.pending[lease['leaseId']] = {
            'ticketHash': hashlib.sha256(ticket.encode()).digest(), 'until': time.time() + 35,
            'actor': snapshot_actor,
            'settings': snapshot, 'url': url, 'lease': dict(lease),
            'applicationTurns': getattr(config, 'yandex_realtime_turn_mode', 'server_vad') == 'application',
        }
        return web.json_response({'transport': 'websocket', 'sampleRate': RATE,
            'path': '/voice-transport/' + lease['leaseId'], 'connectionToken': ticket,
            'leaseId': lease['leaseId'], 'model': model,
            'maxSeconds': max(1, int(lease['expiresAt'] - time.time())),
            'revision': current['revision']}, headers={'Cache-Control': 'no-store'})

    async def current_actor(self, uid, roles, cookie=''):
        async with self.api.engine.connect() as conn:
            people = await self.api.rows(conn, 'SELECT active FROM users WHERE id=:uid', uid=uid)
            found = await self.api.rows(conn, 'SELECT role FROM user_roles WHERE user_id=:uid', uid=uid)
            if cookie:
                from . import miniapp_sessions
                session = await miniapp_sessions.lookup(conn, cookie, time.time())
                if not session or not cookie.startswith(str(uid) + '.'):
                    return False
        return bool(people and people[0]['active']
                    and sorted(x['role'] for x in found) == roles
                    and STAFF_ROLES.intersection(roles))

    async def close(self, lease):
        key = lease['leaseId']
        self.pending.pop(key, None)
        live = self.active.get(key)
        if live:
            await live['upstream'].close()
            await live['browser'].close(code=1000, message=b'closed')
        if (lease.get('workerHost') == socket.gethostname()
                and type(lease.get('workerPid')) is int and lease['workerPid'] > 1):
            try:
                os.kill(lease['workerPid'], 0)  # Existence check, never a signal.
            except ProcessLookupError:
                return True  # The dead process has already closed its sockets.
            except PermissionError:
                pass
        # Another web worker must let the owning worker observe 'closing'.
        return (lease.get('worker') == self.parent.worker_id
                or lease.get('expiresAt', 0) + 5 < time.time())

    async def sweep(self):
        for key, pending in list(self.pending.items()):
            if pending['until'] < time.time():
                if await self.parent.close(pending['actor']['id'], pending['lease']):
                    self.pending.pop(key, None)

    async def connect(self, request, config):
        origin_guard(request)
        key = request.match_info['lease']
        if not re.fullmatch(r'[a-f0-9]{32}', key) or request.query:
            raise AccessError('Голосовой разговор не найден.', 404)
        tokens = [s.strip() for s in request.headers.get('Sec-WebSocket-Protocol', '').split(',')]
        pending = self.pending.get(key)
        supplied = tokens[1] if len(tokens) == 2 and tokens[0] == 'pb-voice' else ''
        if (not pending or pending.get('consumed') or not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied)
                or pending['until'] < time.time()
                or not hmac.compare_digest(pending['ticketHash'], hashlib.sha256(supplied.encode()).digest())):
            raise AccessError('Голосовое подключение истекло. Начните разговор заново.', 401)
        # A consumed tombstone remains until DB cleanup succeeds. It cannot
        # authenticate again, but expiry can recover a transient DB failure.
        pending['consumed'] = True  # Before any await or provider call.
        actor, lease = pending['actor'], pending['lease']
        uid = actor['id']
        try:
            if not await self.current_actor(uid, actor['roles'], actor.get('authCookie', '')):
                raise AccessError('Доступ сотрудника изменён.', 403)
            async with self.api.engine.connect() as conn:
                stored = await self.parent.record(conn, uid, 'realtime-lease:' + key)
            if not stored or stored['status'] != 'connecting' or stored['expiresAt'] <= time.time():
                raise AccessError('Голосовое соединение уже закрыто.', 409)
        except BaseException:
            with suppress(Exception):
                await asyncio.shield(self.parent.close(uid, lease))
            raise
        browser = web.WebSocketResponse(protocols=('pb-voice',), max_msg_size=64 * 1024,
                                        heartbeat=20, receive_timeout=40)
        trace = aiohttp.TraceConfig()
        async def no_redirect(_session, _context, _params):
            raise AccessError('Не удалось подключить голосовой ИИ.', 503)
        trace.on_request_redirect.append(no_redirect)
        prepared = False
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=15), trace_configs=[trace]) as client:
                async with client.ws_connect(pending['url'], heartbeat=20, max_msg_size=8 * 1024 * 1024,
                        headers={'Authorization': 'Api-Key ' + config.yandex_cloud_api_key}) as upstream:
                    async with asyncio.timeout(15):
                        created = await upstream.receive_json()
                        if created.get('type') != 'session.created':
                            raise AccessError('Не удалось подключить голосовой ИИ.', 503)
                        await upstream.send_json({'type': 'session.update', 'session': pending['settings']})
                        while True:
                            updated = await upstream.receive_json()
                            if updated.get('type') == 'error':
                                raise AccessError('Не удалось настроить голосовой ИИ.', 503)
                            if updated.get('type') == 'session.updated':
                                break
                    if not await self.current_actor(uid, actor['roles'], actor.get('authCookie', '')):
                        raise AccessError('Доступ сотрудника изменён.', 403)
                    async with self.api.engine.begin() as conn:
                        stored = await self.parent.record(conn, uid, 'realtime-lease:' + key, lock=True)
                        if not stored or stored['status'] != 'connecting':
                            raise AccessError('Голосовой разговор уже закрыт.', 409)
                        lease = dict(stored, status='active')
                        await self.parent.write(conn, uid, 'realtime-lease:' + key, lease)
                        await self.parent.write(conn, uid, 'realtime-active', lease)
                        await self.api.audit_write(conn, actor, 'academy_realtime_start', 'training',
                                                   lease['sessionId'], json.dumps({'mode': lease['mode'], 'provider': 'yandex'}))
                    await browser.prepare(request)
                    prepared = True
                    self.pending.pop(key, None)
                    self.active[key] = {'browser': browser, 'upstream': upstream}
                    await browser.send_json({'type': 'pb.voice.ready'})
                    await self.bridge(browser, upstream, actor, lease,
                                      application_turns=pending['applicationTurns'])
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AccessError):
            if prepared and not browser.closed:
                with suppress(Exception):
                    await browser.send_json({'type': 'error', 'error': {'message': 'Голосовая связь прервалась. Остановите разговор и подключитесь повторно.'}})
            elif not prepared:
                raise AccessError('Не удалось подключить голосовой ИИ. Повторите подключение.', 503) from None
        finally:
            self.active.pop(key, None)
            with suppress(Exception):
                await asyncio.shield(self.parent.close(uid, lease))
            if prepared:
                await browser.close()
        return browser

    async def bridge(self, browser, upstream, actor, lease, *, application_turns=False):
        uploaded, messages, began = 0, 0, time.monotonic()
        greeting_sent = False
        known_items = set()
        tasks = []
        from .audio_turns import AudioTurns
        turns = AudioTurns() if application_turns else None
        turn_lock = asyncio.Lock()
        async def send_actions(actions):
            for kind, raw in actions:
                if kind == 'audio':
                    await upstream.send_json({'type': 'input_audio_buffer.append',
                                              'audio': base64.b64encode(raw).decode()})
                elif kind in {'speech_started', 'speech_stopped'}:
                    await browser.send_json({'type': 'input_audio_buffer.' + kind})
                elif kind == 'commit':
                    await upstream.send_json({'type': 'input_audio_buffer.commit'})
                elif kind == 'response':
                    await upstream.send_json({'type': 'response.create'})
        async def paused_input():
            while True:
                await asyncio.sleep(0.1)
                async with turn_lock:
                    await send_actions(turns.poll(time.monotonic()))
        async def uplink():
            nonlocal uploaded, greeting_sent, messages
            async for message in browser:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        raise AccessError('Некорректная голосовая запись.', 400)
                    break
                body = json.loads(message.data)
                messages += 1
                if messages > (time.monotonic() - began + 3) * 100:
                    raise ValueError('event_limit')
                if not isinstance(body, dict):
                    raise ValueError('invalid_event')
                kind = body.get('type')
                if kind == 'input_audio_buffer.append' and set(body) == {'type', 'audio'}:
                    value = body['audio']
                    if not isinstance(value, str) or len(value) > MAX_FRAME * 2:
                        raise ValueError('invalid_audio')
                    raw = base64.b64decode(value, validate=True)
                    uploaded += len(raw)
                    seconds = min(600, time.monotonic() - began + 3)
                    if not raw or len(raw) % 2 or len(raw) > MAX_FRAME or uploaded > seconds * RATE * 2:
                        raise ValueError('audio_limit')
                    if turns:
                        async with turn_lock:
                            await send_actions(turns.feed(raw, time.monotonic()))
                    else:
                        await upstream.send_json({'type': kind, 'audio': value})
                elif kind == 'response.create' and not greeting_sent:
                    greeting_sent = True
                    text = ('Коротко представься ИИ-наставником Photo Boss и задай один вопрос: что сотрудник хочет отработать сегодня?'
                            if lease['mode'] == 'coach' else 'Коротко начни диалог в роли гостя по выбранному сценарию. Дождись ответа сотрудника.')
                    await upstream.send_json({'type': kind, 'response': {'instructions': text}})
                elif kind == 'response.cancel' and set(body) <= {'type', 'response_id'}:
                    await upstream.send_json({'type': kind})
                elif kind == 'conversation.item.truncate' and set(body) == {'type', 'item_id', 'content_index', 'audio_end_ms'}:
                    if (not isinstance(body['item_id'], str) or body['item_id'] not in known_items or body['content_index'] != 0
                            or type(body['audio_end_ms']) is not int or not 0 <= body['audio_end_ms'] <= 600000):
                        raise ValueError('invalid_truncate')
                    await upstream.send_json(body)
                else:
                    raise ValueError('unsupported_event')
        async def downlink():
            async for message in upstream:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                body = json.loads(message.data)
                kind = body.get('type') if isinstance(body, dict) else None
                if kind == 'error':
                    code = body.get('error', {}).get('code')
                    if code == 'response_cancel_not_active':
                        continue
                    await browser.send_json({'type': 'error', 'error': {'message': 'Голосовой ИИ временно недоступен.'}})
                    break
                if kind in {'session.created', 'session.updated', 'rate_limits.updated'}:
                    continue
                if kind == 'response.output_item.added':
                    item = body.get('item', {})
                    if item.get('type') == 'message' and item.get('role') == 'assistant':
                        if isinstance(item.get('id'), str):
                            known_items.add(item['id'])
                if kind:
                    await browser.send_json(body)
        async def authority():
            while True:
                await asyncio.sleep(3)
                if not await self.current_actor(actor['id'], actor['roles'], actor.get('authCookie', '')):
                    return
                async with self.api.engine.connect() as conn:
                    stored = await self.parent.record(conn, actor['id'], 'realtime-lease:' + lease['leaseId'])
                if not stored or stored['status'] != 'active' or stored['expiresAt'] <= time.time():
                    return
        try:
            tasks = [asyncio.create_task(fn()) for fn in (uplink, downlink, authority)]
            if turns:
                tasks.append(asyncio.create_task(paused_input()))
            done, _ = await asyncio.wait(tasks, timeout=max(0, lease['expiresAt'] - time.time()),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if turns:
                turns.close()
            await upstream.close()
