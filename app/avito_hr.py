"""Photo Boss Avito recruiting: package-only publication and a bounded HR worker."""
import asyncio
import contextlib
import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aiohttp import web
from sqlalchemy import text

from .avito_dialogue import message_text, ordered, respond, validate_terms

log = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status, data=None):
        super().__init__(f"Avito HTTP {status}")
        self.status, self.data = status, data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def transport_diagnostics(error):
    """Return classifications only: exception messages can contain credentials."""
    reason = error
    seen = set()
    while isinstance(reason, urllib.error.URLError) and id(reason) not in seen:
        seen.add(id(reason))
        nested = reason.reason
        if nested is reason:
            break
        reason = nested
    number = getattr(reason, 'errno', None)
    number = number if type(number) is int else None
    if isinstance(reason, socket.gaierror):
        stage = 'dns'
    elif isinstance(reason, ssl.SSLError):
        stage = 'tls'
    elif isinstance(reason, TimeoutError):
        # A socket timeout alone cannot distinguish connection from read/TLS.
        stage = 'timeout'
    else:
        stage = 'transport'
    return {'stage': stage, 'reason_type': type(reason).__name__, 'errno': number}


def avito_connection(address, timeout=25, source_address=None, **kwargs):
    # Standard DNS fallback, with a short TCP timeout per address. Keep the
    # original read/TLS timeout and HTTPS hostname/certificate verification.
    sock = socket.create_connection(address, timeout=min(timeout, 4), source_address=source_address, **kwargs)
    sock.settimeout(timeout)
    return sock


class AvitoHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = avito_connection


class AvitoHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(AvitoHTTPSConnection, request, context=self._context)


class API:
    def __init__(self):
        self.token = None
        self.expiry = 0
        self.opener = urllib.request.build_opener(NoRedirect(), AvitoHTTPSHandler())

    def call(self, method, path, body=None):
        if time.time() >= self.expiry:
            data = urllib.parse.urlencode({"grant_type": "client_credentials",
                "client_id": os.environ["AVITO_CLIENT_ID"].strip(),
                "client_secret": os.environ["AVITO_CLIENT_SECRET"].strip()}).encode()
            with self.opener.open(urllib.request.Request("https://api.avito.ru/token", data=data), timeout=25) as r:
                auth = json.load(r)
            self.token = auth.get("access_token")
            if not self.token:
                raise APIError(401)
            self.expiry = time.time() + min(int(auth.get("expires_in", 3600)), 86400) - 60
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(urllib.request.Request("https://api.avito.ru" + path,
                    data=data, headers=headers, method=method), timeout=25) as r:
                raw = r.read(4000000)
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(32000))
            except (ValueError, OSError):
                detail = None
            raise APIError(error.code, detail) from None

    async def request(self, method, path, body=None):
        # Only reads may retry. POSTs (including messages) remain one attempt.
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(self.call, method, path, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                log.warning("Avito transport failure method=%s endpoint=%s attempt=%s kind=%s", method, path.split('?')[0].split('/accounts/')[0], attempt + 1, type(exc).__name__)
                if method == 'GET':
                    diagnostic = transport_diagnostics(exc)
                    log.warning('Avito read transport diagnostic stage=%s reason_type=%s errno=%s',
                                diagnostic['stage'], diagnostic['reason_type'], diagnostic['errno'])
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(2 * (attempt + 1))


DDL = [
    """CREATE TABLE IF NOT EXISTS hr_avito_vacancies (
        code TEXT PRIMARY KEY, role TEXT NOT NULL, payload_hash TEXT NOT NULL,
        publication_id TEXT, item_id BIGINT, url TEXT, status TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS hr_avito_outbox (
        event_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, response TEXT NOT NULL,
        state TEXT NOT NULL, message_id TEXT, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS hr_avito_control (
        id INTEGER PRIMARY KEY, paused INTEGER NOT NULL, reason TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS hr_avito_escalations (
        event_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS hr_avito_terms (
        vacancy_code TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS hr_avito_sessions (
        chat_id TEXT PRIMARY KEY, item_id BIGINT, vacancy_code TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
        profile TEXT NOT NULL DEFAULT '{}', transcript TEXT NOT NULL DEFAULT '[]',
        ignored_outgoing TEXT NOT NULL DEFAULT '[]',
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS hr_avito_inbox (
        chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
        PRIMARY KEY(chat_id,message_id))""",
]


async def prepare(engine):
    async with engine.begin() as conn:
        for statement in DDL:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO hr_avito_control VALUES (1,0,'') ON CONFLICT (id) DO NOTHING"))
        # Reuse only facts tied to the exact already-published approval payload.
        # Never seed another vacancy or overwrite an owner's newer settings.
        root = Path(__file__).parent
        seeds_path = root / 'avito_hr_terms_20261020.json'
        payloads_path = root / 'avito_vacancies_20261020.json'
        if seeds_path.is_file() and payloads_path.is_file():
            payloads = {p['vacancy_code']: p for p in json.loads(payloads_path.read_text())}
            for seed in json.loads(seeds_path.read_text()):
                code = seed['vacancy_code']
                source = payloads.get(code)
                if source is None:
                    continue
                digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                await conn.execute(text('''INSERT INTO hr_avito_terms(vacancy_code,payload,revision,confirmed)
                    SELECT code,:payload,1,1 FROM hr_avito_vacancies
                    WHERE code=:code AND payload_hash=:hash AND status='activated'
                    ON CONFLICT(vacancy_code) DO NOTHING'''),
                    {'code': code, 'hash': digest,
                     'payload': json.dumps(validate_terms(seed['terms']), ensure_ascii=False)})


def duplicate(item, payload):
    title = item.get("title", "").lower().replace("ё", "е")
    target = payload["title"].lower().replace("ё", "е")
    city = "анап" in item.get("address", "").lower()
    role_match = ("фотограф" in title if "photographer" in payload["vacancy_code"]
                  else "менеджер" in title and any(w in title for w in ("фото", "запис")))
    return title == target or (city and role_match)


async def own_items(api):
    rows = []
    for page in range(1, 21):
        b = await api.request("GET", "/core/v1/items?category=111&status=active,removed,old,blocked,rejected&per_page=99&page=" + str(page))
        batch = b["resources"]
        if any(x.get("category", {}).get("id") != 111 for x in batch):
            raise ValueError("Unexpected category in duplicate check")
        rows.extend(batch)
        if len(batch) < 99:
            return rows
        await asyncio.sleep(3)
    raise ValueError("Duplicate check pagination limit")


async def refresh_publications(engine, api):
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT code,publication_id FROM hr_avito_vacancies WHERE publication_id IS NOT NULL AND status IN ('submitted','process','new','created','unblocked')"))).mappings().all()
    if not rows:
        return
    statuses = await api.request("POST", "/job/v2/vacancies/statuses", {"ids": [r["publication_id"] for r in rows]})
    for item in statuses:
        vacancy = item.get("vacancy") or {}
        action = item.get("last_action") or {}
        status = "error" if action.get("status") == "error" else vacancy.get("status", action.get("status", "process"))
        if action.get("status") == "success" and vacancy.get("status") == "activated":
            status = "activated"
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_vacancies SET item_id=:item,url=:url,status=:status,details=:details WHERE publication_id=:pid"),
                {"item": vacancy.get("id"), "url": vacancy.get("url"), "status": status,
                 "details": json.dumps(item, ensure_ascii=False), "pid": item["id"]})


async def publish(engine, payloads):
    await prepare(engine)
    api = API()
    existing = await own_items(api)
    for payload in payloads:
        if payload.get("billing_type") != "package" or "salary" in payload or payload.get("schedule") not in (None, "shift"):
            raise ValueError("Only approved package placement, without invented salary or schedule")
        code = payload["vacancy_code"]
        role = "PHOTOGRAPHER" if "photographer" in code else "MANAGER"
        matches = [x for x in existing if duplicate(x, payload)]
        if matches:
            print("AVITO_PUBLICATION", json.dumps({"code": code, "duplicate": [{k: x.get(k) for k in ("id", "title", "url", "status")} for x in matches]}, ensure_ascii=False))
            continue
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with engine.begin() as conn:
            result = await conn.execute(text("INSERT INTO hr_avito_vacancies (code,role,payload_hash,status) VALUES (:code,:role,:hash,'reserved') ON CONFLICT (code) DO NOTHING RETURNING code"), {"code": code, "role": role, "hash": digest})
            reserved = result.scalar()
            if not reserved:
                previous = (await conn.execute(text("SELECT publication_id,status,payload_hash,details FROM hr_avito_vacancies WHERE code=:code"), {"code": code})).mappings().one()
                known_validation_failure = previous["status"] == "error" and not previous["publication_id"] and json.loads(previous["details"]).get("http") == 400
                if known_validation_failure and previous["payload_hash"] != digest:
                    # A definitive pre-creation 400 may be corrected; uncertain writes never retry.
                    reserved = (await conn.execute(text("UPDATE hr_avito_vacancies SET status='reserved',payload_hash=:hash WHERE code=:code AND status='error' AND publication_id IS NULL AND payload_hash=:previous RETURNING code"), {"code": code, "hash": digest, "previous": previous["payload_hash"]})).scalar()
        if not reserved:
            print("AVITO_PUBLICATION", json.dumps({"code": code, "state": "existing_journal_no_resubmit"}))
            continue
        try:
            response = await api.request("POST", "/job/v2/vacancies", payload)
            pid = response["id"]
        except APIError as error:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE hr_avito_vacancies SET status='error',details=:details WHERE code=:code"), {"code": code, "details": json.dumps({"http": error.status, "error": error.data}, ensure_ascii=False)})
            print("AVITO_PUBLICATION", json.dumps({"code": code, "http": error.status, "error": error.data}, ensure_ascii=False))
            if error.status in (401, 402, 403, 429) or error.status >= 500:
                break
            continue
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_vacancies SET publication_id=:pid,status='submitted' WHERE code=:code"), {"pid": pid, "code": code})
        print("AVITO_PUBLICATION", json.dumps({"code": code, "publication_id": pid, "status": "submitted"}))
        await refresh_publications(engine, api)
    await refresh_publications(engine, api)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT code,publication_id,item_id,url,status,details FROM hr_avito_vacancies"))).mappings().all()
    print("AVITO_PUBLICATION_RESULTS", json.dumps([dict(r) for r in rows], ensure_ascii=False))


DIALOGUE_VERSION = "vacancy-profile-v6"


def reply(value, role, history=(), terms=None):
    """Compatibility entry point for integrations using the previous helper."""
    turn = respond(value, role, history, terms)
    return turn.answer, turn.escalate


class Worker:
    def __init__(self, engine):
        self.engine, self.api = engine, API()
        self.ready = False
        self.last_check = None
        self.last_probe = None
        self.configured = True

    async def session(self, chat):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text('SELECT * FROM hr_avito_sessions WHERE chat_id=:chat'),
                                      {'chat': chat})).mappings().first()
        return dict(row) if row else None

    async def terms(self, code):
        async with self.engine.connect() as conn:
            payload = await conn.scalar(text('SELECT payload FROM hr_avito_terms WHERE vacancy_code=:code AND confirmed=1'), {'code': code})
        return json.loads(payload) if payload else {}

    async def save_session(self, chat, role, code, item_id, turn, messages):
        # Never overwrite a manager's durable pause with an in-flight bot turn.
        transcript = [{'id': str(m.get('id', '')), 'direction': m.get('direction'),
                       'type': m.get('type'), 'created': m.get('created'),
                       'text': message_text(m)[:4000]} for m in ordered(messages)[-200:]]
        async with self.engine.begin() as conn:
            await conn.execute(text('''INSERT INTO hr_avito_sessions
                (chat_id,item_id,vacancy_code,role,status,profile,transcript)
                VALUES (:chat,:item,:code,:role,:status,:profile,:transcript)
                ON CONFLICT(chat_id) DO UPDATE SET item_id=excluded.item_id,
                vacancy_code=excluded.vacancy_code,role=excluded.role,
                status=CASE WHEN hr_avito_sessions.status IN ('HUMAN','STOPPED')
                    THEN hr_avito_sessions.status ELSE excluded.status END,
                profile=excluded.profile,transcript=excluded.transcript,updated_at=CURRENT_TIMESTAMP'''),
                {'chat': chat, 'item': item_id, 'code': code, 'role': role, 'status': turn.status,
                 'profile': json.dumps(turn.profile, ensure_ascii=False),
                 'transcript': json.dumps(transcript, ensure_ascii=False)})

    async def owner_notice(self, chat, status):
        # App inbox only. Candidate details remain behind the HR role check.
        event = 'avito-hr:' + hashlib.sha256((chat + ':' + status).encode()).hexdigest()
        async with self.engine.begin() as conn:
            await conn.execute(text('''INSERT INTO notifications
                (user_id,text,sent,created_at,event_key,priority,kind,payload)
                SELECT DISTINCT u.id,:text,FALSE,CURRENT_TIMESTAMP,:event,'info','service_status',:payload
                FROM users u JOIN user_roles r ON r.user_id=u.id
                WHERE u.active=TRUE AND r.role='OWNER'
                ON CONFLICT(user_id,event_key) DO NOTHING'''),
                {'text': 'Авито: анкета кандидата требует внимания. Откройте кабинет HR /app/hr-avito.html.',
                 'event': event, 'payload': json.dumps({'url': '/app/hr-avito.html'})})

    async def pause(self, reason):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_control SET paused=1,reason=:reason WHERE id=1"), {"reason": reason})

    async def send_once(self, key, chat, response, input_ids=()):
        if len(response) > 1000:
            raise ValueError("Response length")
        async with self.engine.begin() as conn:
            lock = ' FOR UPDATE' if conn.dialect.name == 'postgresql' else ''
            state = await conn.scalar(text('SELECT status FROM hr_avito_sessions WHERE chat_id=:chat' + lock), {'chat': chat})
            if state in {'HUMAN', 'STOPPED'}:
                return False
            inserted = (await conn.execute(text("INSERT INTO hr_avito_outbox(event_key,chat_id,response,state) VALUES (:key,:chat,:response,'uncertain') ON CONFLICT(event_key) DO NOTHING RETURNING event_key"), {"key": key, "chat": chat, "response": response})).scalar()
            if inserted:
                for mid in input_ids:
                    await conn.execute(text('INSERT INTO hr_avito_inbox(chat_id,message_id) VALUES (:chat,:mid) ON CONFLICT(chat_id,message_id) DO NOTHING'), {'chat': chat, 'mid': str(mid)})
        if not inserted:
            return False
        result = await self.api.request("POST", f"/messenger/v1/accounts/{self.uid}/chats/{urllib.parse.quote(chat, safe='')}/messages", {"type": "text", "message": {"text": response}})
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_outbox SET state='sent',message_id=:mid WHERE event_key=:key"), {"mid": str(result["id"]), "key": key})
        return True

    async def cycle(self):
        async with self.engine.connect() as conn:
            control = (await conn.execute(text("SELECT paused,reason FROM hr_avito_control WHERE id=1"))).mappings().one()
        if control['paused']:
            if control['reason'] not in {'URLError', 'TimeoutError', 'ConnectionError', 'http_500', 'http_502', 'http_503', 'http_504'}:
                return
            # Probe only: never replay a possibly delivered send. Credentials stay
            # server-side, and a failed probe leaves the durable pause untouched.
            self.last_probe = int(time.time())
            account = await self.api.request("GET", "/core/v1/accounts/self")
            if not account.get('id'):
                raise ValueError("Invalid recovery account")
            async with self.engine.begin() as conn:
                await conn.execute(text("UPDATE hr_avito_control SET paused=0,reason='' WHERE id=1 AND reason=:reason"), {'reason': control['reason']})
            log.info("Avito HR connection recovered after account probe")
        await refresh_publications(self.engine, self.api)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT code,item_id,role FROM hr_avito_vacancies WHERE item_id IS NOT NULL AND status='activated'"))).mappings().all()
        if not rows:
            self.last_check = int(time.time())
            return
        roles = {int(r["item_id"]): dict(r) for r in rows}
        self.uid = (await self.api.request("GET", "/core/v1/accounts/self"))["id"]
        # Messenger is the source of dialogue coverage: applications may contain
        # only a system event/call and no candidate-authored text.
        offset = 0
        while True:
            query = urllib.parse.urlencode({"item_ids": ",".join(map(str, roles)),
                "chat_types": "u2i", "limit": 100, "offset": offset})
            result = await self.api.request("GET", f"/messenger/v2/accounts/{self.uid}/chats?" + query)
            chats = result.get("chats", []) if isinstance(result, dict) else result
            for entry in chats:
                item = (entry.get("context") or {}).get("value") or {}
                try:
                    item_id = int(item.get("id", 0))
                except (TypeError, ValueError):
                    continue
                if item_id not in roles or not entry.get("id"):
                    continue
                vacancy = roles[item_id]
                chat = str(entry['id'])
                try:
                    await self.process_chat(chat, vacancy['role'], vacancy['code'], item_id)
                except (APIError, urllib.error.URLError, TimeoutError, ConnectionError):
                    async with self.engine.connect() as conn:
                        uncertain_send = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE chat_id=:chat AND state='uncertain'"), {'chat': chat})
                    if not uncertain_send:
                        raise
                    log.warning('Avito chat held: delivery result unconfirmed')
                    async with self.engine.begin() as conn:
                        await conn.execute(text("UPDATE hr_avito_sessions SET status='REVIEW' WHERE chat_id=:chat AND status NOT IN ('HUMAN','STOPPED')"), {'chat': chat})
                    await self.owner_notice(chat, 'REVIEW')
            if len(chats) < 100:
                break
            offset += 100
        self.last_check = int(time.time())

    async def read_messages(self, chat):
        messages, offset = [], 0
        while True:
            result = await self.api.request("GET", f"/messenger/v3/accounts/{self.uid}/chats/{urllib.parse.quote(chat, safe='')}/messages/?limit=100&offset={offset}")
            batch = result if isinstance(result, list) else result.get("messages", [])
            messages.extend(batch)
            if len(batch) < 100:
                return messages
            offset += 100

    async def process_chat(self, chat, role, code='', item_id=None):
        session = await self.session(chat)
        if session and session['status'] in {'HUMAN', 'STOPPED'}:
            return
        async with self.engine.connect() as conn:
            blocked = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE chat_id=:chat AND state IN ('stopped','uncertain')"), {"chat": chat})
        if blocked:
            return
        messages = await self.read_messages(chat)
        texts = [m for m in messages if m.get("type") == "text"]
        outgoing = [m for m in texts if m.get("direction") == "out"]
        incoming = [m for m in texts if m.get("direction") == "in"]
        async with self.engine.connect() as conn:
            known = set(await conn.scalars(text("SELECT message_id FROM hr_avito_outbox WHERE chat_id=:chat AND state='sent'"), {'chat': chat}))
        ignored = set(json.loads(session['ignored_outgoing'])) if session else set()
        if any(str(m.get('id')) not in known | ignored for m in outgoing):
            turn = respond('', role, messages, await self.terms(code))
            turn.status = 'HUMAN'
            await self.save_session(chat, role, code, item_id, turn, messages)
            await self.owner_notice(chat, 'HUMAN')
            return
        latest_out = max((int(m.get("created", 0)) for m in outgoing), default=-1)
        async with self.engine.connect() as conn:
            consumed = set(await conn.scalars(text('SELECT message_id FROM hr_avito_inbox WHERE chat_id=:chat'), {'chat': chat}))
        pending = sorted([m for m in incoming if int(m.get("created", 0)) >= latest_out
                          and str(m.get('id')) not in consumed], key=lambda m: int(m.get("created", 0)))
        if pending:
            newest = pending[-1]
            key = "in:" + chat + ":" + str(newest["id"])
            # Respect both normal and previous repair keys, even if API history
            # has not yet reflected the outgoing message.
            async with self.engine.connect() as conn:
                processed = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE event_key IN (:key,:repaired)"), {"key": key, "repaired": "resolved:" + chat + ":" + str(newest["id"])})
            if processed:
                return
            value = "\n".join((m.get("content") or {}).get("text", "") for m in pending)
            pending_ids = {str(m['id']) for m in pending}
            history = [m for m in messages if str(m.get('id')) not in pending_ids]
            turn = respond(value, role, history, await self.terms(code), active=True,
                           profile_state=json.loads(session['profile']) if session else None)
            answer, escalate = turn.answer, turn.escalate
        elif not texts and any(m.get("direction") == "in" and (
                m.get("type") == "appCall" or (m.get("type") == "system" and
                (m.get("content") or {}).get("flow_id") == "job" and
                any(word in (m.get("content") or {}).get("text", "").lower()
                    for word in ("откликнул", "посмотрел ваш телефон")))) for m in messages):
            key = "welcome:" + chat
            turn = respond('Здравствуйте', role, messages, await self.terms(code), active=True)
            answer, escalate = turn.answer, turn.escalate
        else:
            return
        await self.save_session(chat, role, code, item_id, turn, messages)
        if escalate:
            # Persist the handoff before attempting delivery; a send error must
            # not lose the candidate's request or the manager's card.
            async with self.engine.begin() as conn:
                await conn.execute(text("INSERT INTO hr_avito_escalations(event_key,chat_id,reason) VALUES (:key,:chat,'owner_review') ON CONFLICT(event_key) DO NOTHING"), {'key': key, 'chat': chat})
            await self.owner_notice(chat, turn.status)
        if turn.status == 'HUMAN':
            # User asked for a person: no automated message races with takeover.
            return
        if not answer:
            async with self.engine.begin() as conn:
                await conn.execute(text("INSERT INTO hr_avito_outbox(event_key,chat_id,response,state) VALUES (:key,:chat,'','stopped') ON CONFLICT(event_key) DO NOTHING"), {"key": key, "chat": chat})
            return
        # Re-read immediately before reserving the turn: a human or newer input
        # must not be overwritten by a reply generated from stale history.
        fresh = await self.read_messages(chat)
        signature = lambda rows: {(str(m.get("id")), m.get("direction"), message_text(m)) for m in rows if m.get("type") == "text"}
        if signature(fresh) != signature(messages):
            return
        await self.send_once(key, chat, answer, [m['id'] for m in pending])
        await asyncio.sleep(1)

    async def run(self):
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep worker alive and pause on any provider/runtime failure
                reason = "http_" + str(exc.status) if isinstance(exc, APIError) else type(exc).__name__
                await self.pause(reason)
                log.error("Avito HR paused (%s)", reason)
            await asyncio.sleep(60)

    async def health(self, request):
        if not self.configured:
            return web.json_response({'enabled': False, 'ready': False, 'reason': 'credentials_missing'})
        if not self.ready:
            return web.json_response({"enabled": True, "ready": False, "reason": "startup_failed"})
        async with self.engine.connect() as conn:
            control = (await conn.execute(text("SELECT paused,reason FROM hr_avito_control WHERE id=1"))).mappings().one()
            count = await conn.scalar(text("SELECT count(*) FROM hr_avito_vacancies WHERE status='activated'"))
            escalations = await conn.scalar(text("SELECT count(*) FROM hr_avito_escalations WHERE reason NOT LIKE 'resolved%'"))
            sent = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE state='sent'"))
        return web.json_response({"enabled": True, "ready": self.ready, "paused": bool(control["paused"]), "reason": control["reason"], "managedVacancies": count, "lastCheck": self.last_check, "lastProbe": self.last_probe, "sent": sent, "ownerReview": escalations, "dialogueVersion": DIALOGUE_VERSION, "pollSeconds": 60})


def install_avito_hr(app, engine, miniapp=None):
    configured = all(os.getenv(k, "").strip() for k in ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET"))
    worker = Worker(engine)
    worker.configured = configured
    if miniapp is not None:
        from .avito_hr_console import install_console
        install_console(app, miniapp, worker, configured)
    app.router.add_get("/health/avito", worker.health)

    async def lifetime(_app):
        try:
            await prepare(engine)
        except Exception as exc:  # noqa: BLE001 - startup failure must be reported without crashing app startup
            log.error("Avito HR startup failed (%s)", type(exc).__name__)
            yield
            return
        if not configured:
            yield
            return
        worker.ready = True
        task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            worker.ready = False
    app.cleanup_ctx.append(lifetime)
    return worker


