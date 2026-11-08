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

from aiohttp import web
from sqlalchemy import text

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
]


async def prepare(engine):
    async with engine.begin() as conn:
        for statement in DDL:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO hr_avito_control VALUES (1,0,'') ON CONFLICT (id) DO NOTHING"))


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


INTRO = "Здравствуйте! Я автоматический HR-помощник «Фотобосс»."
DIALOGUE_VERSION = "all-dialogues-v4"


def reply(value, role, history=()):
    """Bounded topic recognition with conversation context; unknowns go to owner."""
    import re
    value = value.lower().replace("ё", "е").strip()
    previous = [m for m in sorted(history, key=lambda m: int(m.get("created", 0)))
                if m.get("type") == "text"]
    outbound = [(m.get("content") or {}).get("text", "") for m in previous if m.get("direction") == "out"]
    last = outbound[-1].lower().replace("ё", "е") if outbound else ""
    first = not outbound
    prefix = INTRO + " " if first else ""
    if any(w in value for w in ("не пишите", "не писать", "не беспокойте", "не интересно", "неинтересно", "не подходит", "отказываюсь", "неактуально", "не актуально", "больше не ищу")):
        return "", False
    if "?" not in value and any(w in value for w in ("скину", "пришлю", "отправлю")) and (
            any(w in value for w in ("портфолио", "пример", "работ")) or "портфолио" in last or "примеры" in last):
        return prefix + "Хорошо, буду ждать примеры работ.", False
    if value in {"нет", "нет спасибо", "нет, спасибо"} and any(w in last for w in ("вас интересует", "вам интересн", "актуальна", "подходит")):
        return "", False
    answers = []
    def has(*words):
        return any(word in value for word in words)
    # Concrete questions are handled before greetings, even on the first contact.
    if has("зарплат", "доход", "процент", "оплат", "заработ", "платите", "сколько получ") or (has("сколько", "приблизит", "примерно") and has("месяц", "день", "смен") and any(w in last for w in ("15%", "оплата", "доход", "заработ"))):
        answers.append("Оплата — 15% от продаж, есть бонусы.")
        if has("фикс", "гарант", "в месяц", "за месяц", "в день", "за день", "средн", "сколько можно", "приблизит", "примерно"):
            answers.append("Фиксированную сумму не гарантируем; подтвержденного среднего заработка у меня нет.")
    if has("выплат", "расчет", "расчёт", "когда плат", "как часто плат") or (has("когда", "как часто") and "выплат" in last):
        answers.append("Выплаты еженедельно.")
    if has("бонус", "преми") and not any("есть бонусы" in a for a in answers):
        answers.append("Есть бонусы; правила и суммы уточню у владельца." if has("сколько", "как", "сумм", "расчет") else "Да, есть бонусы.")
    if has("жиль", "жиле", "жили", "прожив", "квартир", "жить", "общежит") or (has("компенсац") and not has("проезд", "билет", "дорог")):
        answers.append("Жильё предоставляем, компания оплачивает 50%. Сейчас стоимость — 10 000 ₽ в месяц: 5 000 ₽ оплачивает компания, 5 000 ₽ — сотрудник.")
    if has("общежит"):
        answers.append("Тип жилья уточню у владельца.")
    if has("проезд", "билет", "переезд", "дорог"):
        answers.append("Проезд компенсируется после приезда и минимум месяца работы; сумму согласует владелец.")
    if has("график", "выходн", "рабочие дни", "3/1", "3 на 1"):
        answers.append("График — 3/1: три рабочих дня, один выходной.")
    if has("час", "во сколько", "длительность смен", "начало смен", "конец смен", "ночн"):
        answers.append("Наличие ночных смен уточню у владельца." if has("ночн") else "Точные часы смен уточню у владельца.")
    if has("где", "адрес", "отел", "место работ", "каком город", "витязево", "анап"):
        answers.append("Работа есть в Анапе и Витязево; конкретное место согласуем с владельцем до выхода.")
    if has("обуч", "опыт", "наставник", "науч", "нович"):
        if has("сколько", "долго", "срок"):
            answers.append("Точный срок обучения уточню у владельца.")
        else:
            answers.append("Можно без опыта: обучение с нуля и помощь наставника." if role == "PHOTOGRAPHER" else "Можно без опыта: обучаем общению с гостями и записи на фотосъемки.")
    if has("обязанност", "что делать", "что надо делать", "чем заним", "что нужно дел") or (has("как это происходит") and any("Менеджер общается" in old or "Фотограф снимает" in old for old in outbound)):
        answers.append("Фотограф снимает гостей; работе с камерой и съемке обучаем." if role == "PHOTOGRAPHER" else "Менеджер общается с гостями, рассказывает о фотосъемках и записывает желающих.")
    ready = bool(re.search(r"(?:готов[аы]?|могу|хочу)\s+(?:выйти|начать|приступить)", value))
    unknown = has("договор", "оформлен", "документ", "налог", "штраф", "питан", "форм", "камера своя", "оборудован", "возраст", "гражданств", "виза")
    needs_owner = unknown or ready or any("уточню" in a for a in answers)
    if ready:
        answers.append("Передам владельцу вашу готовность, чтобы согласовать выход.")
    if unknown:
        answers.append("Этот вопрос уточню у владельца и вернусь с ответом.")
    if answers:
        # One relevant invitation; never repeat a question already asked in history.
        question = ""
        if not needs_owner and len(answers) == 1:
            if "15%" in answers[0]:
                question = "Подходит такой формат оплаты?"
            elif "Можно без опыта" in answers[0]:
                question = "Что хотели бы узнать об обучении?"
            elif "Менеджер общается" in answers[0] or "Фотограф снимает" in answers[0]:
                question = "Вам интересны такие задачи?"
        if question and not any(question in old for old in outbound):
            answers.append(question)
        return prefix + " ".join(dict.fromkeys(answers)), needs_owner
    clean = re.sub(r"[^а-яa-z ]", "", re.sub(r"\\s+", " ", value)).strip()
    if any(w in clean for w in ("заинтересовала", "интересует вакансия")):
        clean = "интересует вакансия"
    if clean in {"здравствуйте", "здравствуй", "добрый день", "добрый вечер", "привет", "интересует вакансия", "заинтересовала ваша вакансия", "менеджер", "фотограф", "актуально", "вакансия актуальна"}:
        if first:
            title = "фотографом" if role == "PHOTOGRAPHER" else "менеджером по записи"
            return prefix + f"Вас интересует работа {title}?", False
        return "Вакансия актуальна. Что хотели бы узнать о работе?", False
    if clean in {"спасибо", "понятно", "хорошо", "ок", "да", "интересно"}:
        if "вас интересует работа" in last:
            return "Что хотели бы узнать о работе?", False
        return "Если появятся вопросы по работе, пишите.", False
    # Never fabricate details or infer a sensitive eligibility decision.
    return prefix + "Уточню ваш вопрос у владельца и вернусь с ответом.", True


class Worker:
    def __init__(self, engine):
        self.engine, self.api = engine, API()
        self.ready = False
        self.last_check = None
        self.last_probe = None

    async def pause(self, reason):
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_control SET paused=1,reason=:reason WHERE id=1"), {"reason": reason})

    async def send_once(self, key, chat, response):
        if len(response) > 1000:
            raise ValueError("Response length")
        async with self.engine.begin() as conn:
            inserted = (await conn.execute(text("INSERT INTO hr_avito_outbox(event_key,chat_id,response,state) VALUES (:key,:chat,:response,'uncertain') ON CONFLICT(event_key) DO NOTHING RETURNING event_key"), {"key": key, "chat": chat, "response": response})).scalar()
        if not inserted:
            return
        result = await self.api.request("POST", f"/messenger/v1/accounts/{self.uid}/chats/{urllib.parse.quote(chat, safe='')}/messages", {"type": "text", "message": {"text": response}})
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE hr_avito_outbox SET state='sent',message_id=:mid WHERE event_key=:key"), {"mid": str(result["id"]), "key": key})

    async def cycle(self):
        async with self.engine.connect() as conn:
            control = (await conn.execute(text("SELECT paused,reason FROM hr_avito_control WHERE id=1"))).mappings().one()
            uncertain = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE state='uncertain'"))
        if control['paused']:
            if control['reason'] not in {'URLError', 'TimeoutError', 'ConnectionError', 'http_500', 'http_502', 'http_503', 'http_504'} or uncertain:
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
            rows = (await conn.execute(text("SELECT item_id,role FROM hr_avito_vacancies WHERE item_id IS NOT NULL AND status='activated'"))).mappings().all()
        if not rows:
            self.last_check = int(time.time())
            return
        roles = {int(r["item_id"]): r["role"] for r in rows}
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
                await self.process_chat(str(entry["id"]), roles[item_id])
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

    async def process_chat(self, chat, role):
        async with self.engine.connect() as conn:
            blocked = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE chat_id=:chat AND state IN ('stopped','uncertain')"), {"chat": chat})
        if blocked:
            return
        messages = await self.read_messages(chat)
        texts = [m for m in messages if m.get("type") == "text"]
        outgoing = [m for m in texts if m.get("direction") == "out"]
        incoming = [m for m in texts if m.get("direction") == "in"]
        latest_out = max((int(m.get("created", 0)) for m in outgoing), default=-1)
        pending = sorted([m for m in incoming if int(m.get("created", 0)) > latest_out], key=lambda m: int(m.get("created", 0)))
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
            answer, escalate = reply(value, role, messages)
        elif not texts and any(m.get("direction") == "in" and (
                m.get("type") == "appCall" or (m.get("type") == "system" and
                (m.get("content") or {}).get("flow_id") == "job" and
                any(word in (m.get("content") or {}).get("text", "").lower()
                    for word in ("откликнул", "посмотрел ваш телефон")))) for m in messages):
            key = "welcome:" + chat
            title = "фотографа" if role == "PHOTOGRAPHER" else "менеджера по записи"
            answer, escalate = INTRO + f" Спасибо за отклик на вакансию {title}. Что хотели бы узнать о работе?", False
        else:
            return
        if not answer:
            async with self.engine.begin() as conn:
                await conn.execute(text("INSERT INTO hr_avito_outbox(event_key,chat_id,response,state) VALUES (:key,:chat,'','stopped') ON CONFLICT(event_key) DO NOTHING"), {"key": key, "chat": chat})
            return
        # Re-read immediately before reserving the turn: a human or newer input
        # must not be overwritten by a reply generated from stale history.
        fresh = await self.read_messages(chat)
        signature = lambda rows: {(str(m.get("id")), m.get("direction")) for m in rows if m.get("type") == "text"}
        if signature(fresh) != signature(messages):
            return
        await self.send_once(key, chat, answer)
        async with self.engine.begin() as conn:
            if escalate:
                # Unknown questions are tracked per turn; they never suppress
                # replies to later candidate messages, including known questions.
                await conn.execute(text("INSERT INTO hr_avito_escalations(event_key,chat_id,reason) VALUES (:key,:chat,'owner_review') ON CONFLICT(event_key) DO NOTHING"), {"key": key, "chat": chat})
            else:
                await conn.execute(text("UPDATE hr_avito_escalations SET reason='resolved_dialogue' WHERE chat_id=:chat AND reason='human_takeover'"), {"chat": chat})
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
        if not self.ready:
            return web.json_response({"enabled": True, "ready": False, "reason": "startup_failed"})
        async with self.engine.connect() as conn:
            control = (await conn.execute(text("SELECT paused,reason FROM hr_avito_control WHERE id=1"))).mappings().one()
            count = await conn.scalar(text("SELECT count(*) FROM hr_avito_vacancies WHERE status='activated'"))
            escalations = await conn.scalar(text("SELECT count(*) FROM hr_avito_escalations WHERE reason NOT LIKE 'resolved%'"))
            sent = await conn.scalar(text("SELECT count(*) FROM hr_avito_outbox WHERE state='sent'"))
        return web.json_response({"enabled": True, "ready": self.ready, "paused": bool(control["paused"]), "reason": control["reason"], "managedVacancies": count, "lastCheck": self.last_check, "lastProbe": self.last_probe, "sent": sent, "ownerReview": escalations, "dialogueVersion": DIALOGUE_VERSION, "pollSeconds": 60})


def install_avito_hr(app, engine):
    if not all(os.getenv(k, "").strip() for k in ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET")):
        return None
    worker = Worker(engine)
    app.router.add_get("/health/avito", worker.health)

    async def lifetime(_app):
        try:
            await prepare(engine)
        except Exception as exc:  # noqa: BLE001 - startup failure must be reported without crashing app startup
            log.error("Avito HR startup failed (%s)", type(exc).__name__)
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

