"""Optional SpeechKit transport; credentials and recording conversion stay server-side."""
import asyncio
from io import BytesIO
import json
import os
import shutil
import wave

import aiohttp

from .miniapp_security import AccessError
from .services.ai_diagnostics import http_failure, record, unavailable

RATE = 16000
MAX_SECONDS = 90
STT_MODEL = 'yandex-speechkit-stt'
TTS_MODEL = 'yandex-speechkit-tts'


def headers(config):
    if os.environ.get('PHOTO_BOSS_AI_COMPARE_BUDGET'):
        raise AccessError('Голосовая проверка сейчас недоступна. Диалог сохранён.', 503)
    key = getattr(config, 'yandex_cloud_api_key', '')
    folder = getattr(config, 'yandex_cloud_folder_id', '')
    if not key or not folder:
        raise AccessError('Голосовой ИИ не подключён. Обратитесь к владельцу.', 503)
    return {'Authorization': 'Api-Key ' + key}


async def response_bytes(response, purpose, model, limit):
    if response.status != 200:
        try:
            body = await response.json()
        except (ValueError, aiohttp.ContentTypeError):
            body = {}
        failure = http_failure(purpose, model, response.status, body)
        raise AccessError(failure['message'].replace('OpenAI', 'Яндекс SpeechKit'), 503)
    data = bytearray()
    async for piece in response.content.iter_chunked(65536):
        data.extend(piece)
        if len(data) > limit:
            raise AccessError('Голосовой ответ слишком большой. Повторите запрос.', 503)
    return bytes(data)


def wav(pcm):
    target = BytesIO()
    with wave.open(target, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(pcm)
    return target.getvalue()


async def decode_recording(raw, mime):
    """Convert the existing Safari/Chrome recordings with finite CPU/output bounds."""
    if mime in {'audio/wav', 'audio/x-wav'}:
        try:
            with wave.open(BytesIO(raw), 'rb') as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                        source.getcomptype()) == (1, 2, RATE, 'NONE'):
                    value = source.readframes(RATE * (MAX_SECONDS + 1))
                    if not value or len(value) > RATE * 2 * MAX_SECONDS:
                        raise AccessError('Запишите непустую реплику до 90 секунд.', 400)
                    return value
        except (wave.Error, EOFError):
            pass
    executable = shutil.which('ffmpeg')
    if not executable:
        raise AccessError('Преобразование голосовых записей ещё не настроено. Обратитесь к владельцу.', 503)
    process = await asyncio.create_subprocess_exec(
        executable, '-v', 'error', '-nostdin', '-protocol_whitelist', 'pipe',
        '-i', 'pipe:0', '-map', '0:a:0', '-vn', '-ac', '1', '-ar', str(RATE),
        '-t', '90.1', '-f', 's16le', 'pipe:1', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(raw), timeout=25)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if (process.returncode or not output or len(output) % 2
            or len(output) > RATE * 2 * MAX_SECONDS):
        raise AccessError('Не удалось прочитать запись. Запишите реплику до 90 секунд ещё раз.', 400)
    return output


def chunks(pcm):
    """Choose quiet boundaries near 30 seconds; a 90-second turn needs at most four."""
    size = RATE * 2 * 30
    while len(pcm) > size:
        # Find the lowest-energy 20ms interval in the last half second.
        candidates = range(size - RATE, size, RATE // 25 * 2)
        def energy(offset):
            part = pcm[offset:offset + RATE // 50 * 2]
            return sum(abs(int.from_bytes(part[i:i+2], 'little', signed=True))
                       for i in range(0, len(part), 2))
        split = min(candidates, key=energy)
        yield pcm[:split]
        pcm = pcm[split:]
    if pcm:
        yield pcm


async def transcribe(raw, mime, config):
    auth = headers(config)
    purpose = 'voice_transcription'
    try:
        pcm = await decode_recording(raw, mime)
        pieces = []
        async with asyncio.timeout(60), aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),
                                         cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as client:
            for chunk in chunks(pcm):
                async with client.post('https://stt.api.cloud.yandex.net/speech/v1/stt:recognize',
                        headers=auth, params={'lang': 'ru-RU', 'format': 'lpcm',
                        'sampleRateHertz': str(RATE), 'rawResults': 'true',
                        'folderId': config.yandex_cloud_folder_id}, data=chunk,
                        allow_redirects=False) as response:
                    body = json.loads(await response_bytes(response, purpose, STT_MODEL, 32 * 1024))
                value = body.get('result') if isinstance(body, dict) else None
                if not isinstance(value, str):
                    raise ValueError('invalid_output')
                if value.strip():
                    pieces.append(value.strip())
        result = ' '.join(pieces)
        if not result:
            raise AccessError('Речь не распознана. Запишите ответ ещё раз.', 422)
        if len(result) > 2000:
            raise AccessError('Ответ слишком длинный. Запишите короткую реплику до 90 секунд.', 400)
        record(purpose, STT_MODEL, 'completed')
        return result
    except AccessError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        failure = unavailable(purpose, STT_MODEL,
            'timeout' if isinstance(error, TimeoutError) else
            'invalid_output' if isinstance(error, ValueError) else 'network_error')
        raise AccessError(failure['message'].replace('OpenAI', 'Яндекс SpeechKit') + ' Запись доступна для повтора, пока открыта эта страница.', 503) from None


async def synthesize(message, config):
    auth = headers(config)
    purpose = 'voice_speech'
    try:
        pcm = bytearray()
        speech = message[:4000]
        async with asyncio.timeout(60), aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),
                                         cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as client:
            # UTF-8 remains under the v1 request limit, including Russian text.
            for offset in range(0, len(speech), 1800):
                async with client.post('https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize',
                        headers=auth, data={'text': speech[offset:offset+1800], 'lang': 'ru-RU',
                        'voice': 'jane', 'format': 'lpcm', 'sampleRateHertz': str(RATE),
                        'folderId': config.yandex_cloud_folder_id}, allow_redirects=False) as response:
                    pcm.extend(await response_bytes(response, purpose, TTS_MODEL, 6 * 1024 * 1024))
                if len(pcm) > 6 * 1024 * 1024:
                    raise ValueError('invalid_output')
        if not pcm or len(pcm) % 2:
            raise ValueError('invalid_output')
        record(purpose, TTS_MODEL, 'completed')
        return wav(pcm)
    except AccessError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        failure = unavailable(purpose, TTS_MODEL,
            'timeout' if isinstance(error, TimeoutError) else
            'invalid_output' if isinstance(error, ValueError) else 'network_error')
        raise AccessError(failure['message'].replace('OpenAI', 'Яндекс SpeechKit') + ' Диалог сохранён.', 503) from None
