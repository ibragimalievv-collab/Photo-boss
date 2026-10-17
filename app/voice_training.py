"""Private voice turns for the existing persisted Academy conversations."""
import hashlib
import json
import time

import aiohttp
from aiohttp import web

from .config import config
from .miniapp_security import AccessError
from .models import utc_now
from .people import positive_id

MAX_AUDIO = 10 * 1024 * 1024
AUDIO_TYPES = {'audio/webm': 'webm', 'video/webm': 'webm', 'audio/mp4': 'mp4',
               'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav'}


async def transcribe(raw, mime):
    if not config.openai_api_key:
        raise AccessError('Голосовой ИИ не подключён. Обратитесь к владельцу.', 503)
    form = aiohttp.FormData()
    form.add_field('model', 'gpt-4o-mini-transcribe')
    form.add_field('language', 'ru')
    form.add_field('file', raw, filename='voice.'+AUDIO_TYPES[mime], content_type=mime)
    try:
        async with (aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client,
                    client.post('https://api.openai.com/v1/audio/transcriptions',
                    headers={'Authorization': f'Bearer {config.openai_api_key}'}, data=form) as response):
            if response.status != 200:
                raise AccessError('Не удалось распознать голос. Запись сохранена на устройстве — повторите отправку.', 503)
            body = await response.json()
        value = body.get('text')
        if not isinstance(value, str) or not value.strip():
            raise AccessError('Речь не распознана. Запишите ответ ещё раз.', 422)
        if len(value.strip()) > 2000:
            raise AccessError('Ответ слишком длинный. Запишите короткую реплику до 90 секунд.', 400)
        return value.strip()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise AccessError('Сервис распознавания недоступен. Голосовая запись остаётся для повтора.', 503) from exc

async def synthesize(message):
    if not config.openai_api_key:
        raise AccessError('Голосовой ИИ не подключён.', 503)
    try:
        async with (aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client,
                    client.post('https://api.openai.com/v1/audio/speech',
                    headers={'Authorization': f'Bearer {config.openai_api_key}'},
                    json={'model': 'gpt-4o-mini-tts', 'voice': 'coral', 'input': message[:4000],
                          'response_format': 'mp3', 'instructions': 'Говори естественно по-русски, как собеседник в учебной тренировке.'}) as response):
            if response.status != 200:
                raise AccessError('Озвучка временно недоступна. Диалог сохранён — повторите прослушивание.', 503)
            audio = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                audio.extend(chunk)
                if len(audio) > 6 * 1024 * 1024:
                    raise AccessError('Голосовой ответ слишком большой. Повторите прослушивание.', 503)
            if not audio:
                raise AccessError('ИИ вернул пустой голосовой ответ.', 503)
            return bytes(audio)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise AccessError('Сервис озвучки недоступен. Диалог сохранён.', 503) from exc

class VoiceTraining:
    def __init__(self, development):
        self.development, self.api = development, development.api
        self.pending = set()
        self.audio_cache = {}

    async def session(self, actor, sid):
        async with self.api.engine.connect() as conn:
            rows = await self.api.rows(conn, 'SELECT * FROM sales_training_sessions WHERE id=:id AND user_id=:uid', id=sid, uid=actor['id'])
        if not rows:
            raise AccessError('Тренировка не найдена.', 404)
        return rows[0]

    async def voice(self, request):
        actor, sid = request['miniapp_actor'], positive_id(request.match_info['id'])
        current = await self.session(actor, sid)
        if actor['id'] in self.pending:
            raise AccessError('Предыдущий голосовой запрос ещё выполняется.', 429)
        self.pending.add(actor['id'])
        try:
            try:
                reader = await request.multipart()
                raw, revision, mime = bytearray(), None, None
                fields = set()
                async for part in reader:
                    if part.name in fields or part.name not in ('audio', 'revision'):
                        raise AccessError('Некорректная голосовая запись.', 400)
                    fields.add(part.name)
                    if part.name == 'revision':
                        value = await part.read_chunk(8192)
                        if not value.isdigit() or len(value) > 9:
                            raise AccessError('Обновите диалог перед записью.', 400)
                        revision = int(value)
                    else:
                        mime = part.headers.get('Content-Type', '').split(';')[0].lower()
                        if mime not in AUDIO_TYPES:
                            raise AccessError('Формат записи не поддерживается. Используйте запись голоса в приложении.', 400)
                        while chunk := await part.read_chunk(65536):
                            raw.extend(chunk)
                            if len(raw) > MAX_AUDIO:
                                raise AccessError('Голосовая запись больше 10 МБ. Запишите короткую реплику.', 413)
            except (ValueError, AssertionError) as exc:
                raise AccessError('Не удалось прочитать голосовую запись.', 400) from exc
            if not raw or revision is None:
                raise AccessError('Диалог обновлён или запись пуста. Откройте текущую тренировку.', 409)
            digest = hashlib.sha256(raw).hexdigest()
            key = f'voice-transcript:{sid}:{revision}:{digest[:24]}'
            async with self.api.engine.connect() as conn:
                saved = await self.api.rows(conn, 'SELECT result FROM operation_requests WHERE user_id=:uid AND request_key=:key', uid=actor['id'], key=key)
            if saved:
                message = json.loads(saved[0]['result'])['text']
            else:
                if revision != current['revision'] or current['status'] != 'ACTIVE':
                    raise AccessError('Диалог обновлён. Откройте текущую тренировку.', 409)
                message = await transcribe(bytes(raw), mime)
                async with self.api.engine.begin() as conn:
                    from sqlalchemy import text
                    await conn.execute(text('''INSERT INTO operation_requests(user_id,request_key,payload_hash,result,created_at)
                        VALUES (:uid,:key,:hash,:result,:now) ON CONFLICT(user_id,request_key) DO NOTHING'''),
                        {'uid': actor['id'], 'key': key, 'hash': digest, 'result': json.dumps({'text': message}, ensure_ascii=False), 'now': utc_now()})
            return await self.development.save_turn(actor, sid, {'revision': revision, 'text': message, 'finish': False}, source='voice')
        finally:
            self.pending.discard(actor['id'])

    async def speech(self, request):
        actor, sid = request['miniapp_actor'], positive_id(request.match_info['id'])
        if await self.api.body(request) != {}:
            raise AccessError('Можно прослушать только сохранённый ответ ИИ.', 400)
        current = await self.session(actor, sid)
        transcript = json.loads(current['transcript'])
        latest = next((t['text'] for t in reversed(transcript) if t.get('role') in ('client', 'coach')), '')
        if current['status'] == 'COMPLETED':
            evaluation = json.loads(current['evaluation'] or '{}')
            latest += ' Ошибки. ' + '. '.join(evaluation.get('errors', []))
            latest += ' Рекомендации. ' + '. '.join(evaluation.get('recommendations', []))
        key = (actor['id'], sid, current['revision'])
        cached = self.audio_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            audio = cached[1]
        else:
            if actor['id'] in self.pending:
                raise AccessError('Предыдущий голосовой запрос ещё выполняется.', 429)
            self.pending.add(actor['id'])
            try:
                audio = await synthesize(latest)
                if len(self.audio_cache) >= 12:
                    self.audio_cache.pop(next(iter(self.audio_cache)))
                self.audio_cache[key] = (time.monotonic(), audio)
            finally:
                self.pending.discard(actor['id'])
        return web.Response(body=audio, content_type='audio/mpeg', headers={'Cache-Control': 'private, no-store'})


def install_voice_training(app, development):
    service = VoiceTraining(development)
    app.router.add_post('/api/miniapp/academy/sales-training/{id}/voice', service.voice)
    app.router.add_post('/api/miniapp/academy/sales-training/{id}/speech', service.speech)
    return service
