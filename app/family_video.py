"""Bounded local MP4 rendering; no photos or family details go to an AI provider."""
import asyncio
import io
import json
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageOps


def available():
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        if process.returncode:
            raise ValueError('Не удалось обработать медиафайл.')
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def validate_audio(raw):
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise ValueError('Музыка: файл MP3, M4A или WAV до 12 МБ.')
    if not available():
        raise ValueError('Создание видео пока недоступно на сервере.')
    with tempfile.TemporaryDirectory(prefix='pb-audio-') as directory:
        path = Path(directory) / 'audio'
        await asyncio.to_thread(path.write_bytes, raw)
        info = json.loads(await command('ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_streams', '-show_format', '-of', 'json', str(path)))
        audio = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
        if (len(audio) != 1 or audio[0].get('codec_name') not in {'mp3', 'aac', 'pcm_s16le', 'pcm_s24le', 'pcm_f32le'}
                or not 1 <= float(info.get('format', {}).get('duration', 0)) <= 600):
            raise ValueError('Нужна музыка MP3, M4A или WAV длительностью до 10 минут.')


def prepare_image(raw, path, size):
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 60_000_000:
            raise ValueError('Изображение слишком большое.')
        image = ImageOps.exif_transpose(image).convert('RGB')
        # Letterbox preserves the entire photograph, including people's faces.
        image.thumbnail(size)
        canvas = Image.new('RGB', size, '#0d1520')
        canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
        canvas.save(path, 'JPEG', quality=92)


async def render(photos, *, format='VERTICAL', audio=None):
    if not available() or not 2 <= len(photos) <= 12:
        raise ValueError('Для слайд-шоу нужно от 2 до 12 фотографий и FFmpeg на сервере.')
    size = (720, 1280) if format == 'VERTICAL' else (1280, 720)
    with tempfile.TemporaryDirectory(prefix='pb-film-') as directory:
        root = Path(directory)
        for index, raw in enumerate(photos):
            frame, segment = root / f'{index}.jpg', root / f'{index}.mp4'
            await asyncio.to_thread(prepare_image, raw, frame, size)
            await command('ffmpeg', '-v', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file,pipe',
                '-i', str(frame), '-vf',
                f'zoompan=z=1:d=72:s={size[0]}x{size[1]}:fps=24,fade=t=in:st=0:d=0.3,fade=t=out:st=2.7:d=0.3,format=yuv420p',
                '-frames:v', '72', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-threads', '1', str(segment))
        manifest = root / 'segments.txt'
        await asyncio.to_thread(manifest.write_text, ''.join(f"file '{index}.mp4'\n" for index in range(len(photos))))
        output = root / 'result.mp4'
        args = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file,pipe',
                '-f', 'concat', '-safe', '1', '-i', str(manifest)]
        if audio:
            music = root / 'music'
            await asyncio.to_thread(music.write_bytes, audio)
            args += ['-stream_loop', '-1', '-i', str(music), '-map', '0:v:0', '-map', '1:a:0',
                     '-c:a', 'aac', '-b:a', '128k', '-af', f'afade=t=out:st={len(photos)*3-1}:d=1']
        args += ['-c:v', 'copy', '-t', str(len(photos)*3), '-movflags', '+faststart', str(output)]
        await command(*args)
        if output.stat().st_size > 40 * 1024 * 1024:
            raise ValueError('Ролик превышает допустимый размер.')
        return await asyncio.to_thread(output.read_bytes)
