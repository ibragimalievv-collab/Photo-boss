"""Optional, isolated recruiting webhook; never uses the employee bot token."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from aiogram.utils.token import TokenValidationError
from aiohttp import web
from sqlalchemy import text

from .models import utc_now

logger = logging.getLogger(__name__)
PATH = '/telegram/hr/webhook'
USERNAME = 'Really_boss_HR_bot'
SOURCE = 'Telegram / Really_boss_HR_bot'
INTRO = ('Работа фотографом или менеджером по записи в Сочи, Анапе и Крыму. '
         'Опыт не обязателен, обучение предоставляется. Оплата — процент, выплаты еженедельно. '
         'Доход не гарантирован. Жильё компенсируется частично. '
         'Проезд компенсируется после приезда и минимум месяца работы. '
         'Для отклика нажмите «Ищу работу».')
CONSENT = ('Анкета будет сохранена в HR-кабинете Photo Boss и доступна владельцу и администратору '
           'для рассмотрения вашего отклика и связи с вами. Не присылайте паспортные и банковские данные. '
           'Нажмите «Согласен», чтобы передать данные, или /cancel для отмены.')
PROMPTS = {
    'name': 'Как вас зовут? Укажите имя (до 150 символов).',
    'role': 'Какая работа интересует: Фотограф или Менеджер по записи?',
    'city': 'Из какого вы города и где хотите работать: Сочи, Анапа или Крым? Можно указать другой регион.',
    'experience': 'Расскажите об опыте. Если опыта нет, так и напишите — мы обучаем.',
    'contact': 'Укажите телефон для связи в международном формате, например +79991234567.',
    'availability': 'Когда готовы приступить и нужен ли переезд?',
}
ORDER = list(PROMPTS)


def secret_for(token):
    return hmac.new(token.encode(), b'photo-boss:hr-webhook:v1', hashlib.sha256).hexdigest()


def keyboard(step):
    options = {'welcome': ['Ищу работу'], 'consent': ['Согласен'],
               'role': ['Фотограф', 'Менеджер по записи'], 'confirm': ['Отправить анкету']}.get(step)
    if not options:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=s)] for s in options], resize_keyboard=True)


class HRBot:
    def __init__(self, engine, bot, secret):
        self.engine, self.bot, self.secret = engine, bot, secret
        self.ready = False

    async def prepare(self):
        # Additive, idempotent schema; existing HR tables and records are untouched.
        async with self.engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS hr_bot_sessions (
                telegram_id BIGINT PRIMARY KEY, step VARCHAR(30) NOT NULL,
                answers TEXT NOT NULL, last_update BIGINT NOT NULL,
                response TEXT NOT NULL, candidate_id INTEGER REFERENCES hr_candidates(id),
                updated_at TIMESTAMP NOT NULL)'''))

    async def receive(self, update):
        msg = update.message
        if not msg or msg.chat.type != 'private' or not msg.from_user or msg.from_user.is_bot:
            return None
        uid = msg.from_user.id
        if uid != msg.chat.id:
            return None
        value = (msg.text or '').strip()
        if msg.contact and msg.contact.user_id == uid:
            value = msg.contact.phone_number
        async with self.engine.begin() as conn:
            if conn.dialect.name == 'postgresql':
                # Serializes even the first insert and across multiple process instances.
                await conn.execute(text('SELECT pg_advisory_xact_lock(:uid)'), {'uid': uid})
            await conn.execute(text('''INSERT INTO hr_bot_sessions
                (telegram_id,step,answers,last_update,response,updated_at)
                VALUES (:uid,'welcome','{}',-1,'',:now) ON CONFLICT (telegram_id) DO NOTHING'''),
                {'uid': uid, 'now': utc_now()})
            row = (await conn.execute(text('SELECT * FROM hr_bot_sessions WHERE telegram_id=:uid'), {'uid': uid})).mappings().one()
            if update.update_id < row['last_update']:
                return None
            if update.update_id == row['last_update']:
                return row['response'], row['step']
            step, answers, cid = row['step'], json.loads(row['answers']), row['candidate_id']
            if cid:
                response = f'Ваша анкета №{cid} уже передана в HR. Повторный отклик не требуется.'
                step = 'done'
            elif value == '/cancel':
                step, answers, response = 'welcome', {}, 'Заполнение отменено, черновик удалён. Для нового отклика нажмите «Ищу работу».'
            elif value.split(' ', 1)[0] == '/start':
                response = INTRO if step == 'welcome' else ('Продолжим вашу анкету. ' + self.prompt(step, answers))
            elif step == 'welcome':
                if value == 'Ищу работу':
                    step = 'consent'
                    response = CONSENT
                else:
                    response = INTRO
            elif step == 'consent':
                if value == 'Согласен':
                    answers = {'consent_at': str(utc_now()), 'telegram_id': uid}
                    step = 'name'
                    response = PROMPTS[step]
                else:
                    response = CONSENT
            elif step == 'confirm':
                if value == 'Отправить анкету':
                    cid = await self.submit(conn, uid, answers)
                    step, answers = 'done', {}
                    response = f'Спасибо! Анкета №{cid} передана в HR Photo Boss. С вами свяжутся после рассмотрения.'
                else:
                    response = self.prompt(step, answers)
            else:
                valid = bool(value) and len(value) <= (150 if step == 'name' else 800)
                if step == 'role':
                    valid = value in {'Фотограф', 'Менеджер по записи'}
                if step == 'contact':
                    value = re.sub(r'[\s()\-]', '', value)
                    valid = bool(re.fullmatch(r'\+?[1-9][0-9]{9,14}', value))
                if not valid:
                    response = 'Проверьте ответ. ' + PROMPTS[step]
                else:
                    answers[step] = value
                    index = ORDER.index(step) + 1
                    step = ORDER[index] if index < len(ORDER) else 'confirm'
                    response = self.prompt(step, answers)
            await conn.execute(text('''UPDATE hr_bot_sessions SET step=:step,answers=:answers,
                last_update=:update,response=:response,candidate_id=:cid,updated_at=:now WHERE telegram_id=:uid'''),
                {'step': step, 'answers': json.dumps(answers, ensure_ascii=False), 'update': update.update_id,
                 'response': response, 'cid': cid, 'now': utc_now(), 'uid': uid})
        return response, step

    def prompt(self, step, answers):
        if step == 'consent':
            return CONSENT
        if step == 'confirm':
            return '\n'.join(['Проверьте анкету:', *[answers[k] for k in ORDER],
                              'Нажмите «Отправить анкету» или /cancel для удаления черновика.'])
        return PROMPTS.get(step, INTRO)

    async def submit(self, conn, uid, answers):
        owner = await conn.scalar(text('''SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id
            WHERE u.active=TRUE AND r.role='OWNER' ORDER BY u.id LIMIT 1'''))
        if owner is None:
            raise RuntimeError('HR requires an existing active owner')
        now = utc_now()
        note = {'actorId': owner, 'at': str(now), 'text': 'Анкета HR-бота: ' + json.dumps(answers, ensure_ascii=False)}
        cid = await conn.scalar(text('''INSERT INTO hr_candidates
            (name,contact,source,role,stage,decision,notes,created_by_id,revision,created_at,updated_at)
            VALUES (:name,:contact,:source,:role,'NEW','',:notes,:owner,1,:now,:now) RETURNING id'''),
            {'name': answers['name'], 'contact': f"{answers['contact']} | tg://user?id={uid}",
             'source': SOURCE, 'role': 'PHOTOGRAPHER' if answers['role'] == 'Фотограф' else 'MANAGER',
             'notes': json.dumps([note], ensure_ascii=False), 'owner': owner, 'now': now})
        await conn.execute(text('''INSERT INTO audit_logs(user_id,action,entity,entity_id,details,created_at)
            VALUES (:owner,'hr_bot_candidate_created','hr_candidate',:cid,:details,:now)'''),
            {'owner': owner, 'cid': cid, 'details': json.dumps({'source': SOURCE, 'telegramId': uid}), 'now': now})
        return cid

    async def webhook(self, request):
        supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(supplied, self.secret):
            raise web.HTTPForbidden()
        if not self.ready:
            raise web.HTTPServiceUnavailable()
        if request.content_length and request.content_length > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length)
        try:
            update = Update.model_validate(await request.json())
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None
        try:
            result = await self.receive(update)
            if result:
                await self.bot.send_message(update.message.chat.id, result[0], reply_markup=keyboard(result[1]), parse_mode=None)
        except TelegramForbiddenError:
            pass  # Blocked bot: don't retry forever. A committed candidate stays saved.
        except Exception as exc:  # noqa: BLE001 -- isolate HR; do not log secret-bearing exceptions
            logger.error('HR update failed (%s); Telegram may retry', type(exc).__name__)
            raise web.HTTPServiceUnavailable() from None
        return web.json_response({'ok': True})

    async def health(self, request):
        return web.json_response({'enabled': True, 'ready': self.ready, 'bot': USERNAME}, status=200 if self.ready else 503)


def install_hr_bot(app, engine, main_token):
    """Disabled by default. No main bot commands, token, dispatcher or menu changes."""
    if os.getenv('HR_BOT_ENABLED', '0') != '1':
        return None
    token = os.getenv('HR_BOT_TOKEN', '').strip()
    # Validate before constructing a client; never log a token or Telegram request URL.
    if not token or token.split(':')[0] == main_token.split(':')[0]:
        logger.error('HR disabled: missing token or same bot identity as main bot')
        return None
    try:
        bot = Bot(token=token)
    except TokenValidationError:
        logger.error('HR disabled: invalid HR_BOT_TOKEN')
        return None
    service = HRBot(engine, bot, secret_for(token))
    app['hr_bot'] = service
    app.router.add_post(PATH, service.webhook)
    app.router.add_get('/health/hr', service.health)

    async def startup(_app):
        try:
            async with asyncio.timeout(20):
                me = await bot.get_me()
                if (me.username or '').lower() != USERNAME.lower():
                    raise ValueError('Unexpected HR bot identity')
                from .launch_policy import app_url
                base = app_url().split('/app/', 1)[0]
                if urlparse(base).scheme != 'https':
                    raise ValueError('HTTPS is required')
                await service.prepare()
                await bot.set_webhook(base + PATH, secret_token=service.secret,
                                      allowed_updates=['message'], drop_pending_updates=False,
                                      max_connections=1)
                service.ready = True
                logger.info('Separate HR bot @%s ready', USERNAME)
        except Exception as exc:  # noqa: BLE001 -- isolate HR; do not log secret-bearing exceptions
            logger.error('HR startup failed (%s); main bot remains independent', type(exc).__name__)

    async def cleanup(_app):
        service.ready = False
        await bot.session.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return service
