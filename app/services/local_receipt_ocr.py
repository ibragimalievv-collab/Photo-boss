"""Bounded local OCR. Documents stay on this server; only text reaches AI."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

MAX_OUTPUT = 128_000
CHILD = '''import os,resource,sys
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
resource.setrlimit(resource.RLIMIT_FSIZE,(32*1024*1024,32*1024*1024))
os.execv(sys.argv[1],sys.argv[1:])
'''


async def command(arguments, directory):
    # OCR binaries have no reason to inherit database, bank or AI credentials.
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'OMP_THREAD_LIMIT': '1', 'OMP_NUM_THREADS': '1',
           'HOME': str(directory), 'TMPDIR': str(directory)}
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', CHILD, *arguments,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=directory, env=env)
    try:
        async with asyncio.timeout(25):
            data = await process.stdout.read(MAX_OUTPUT + 1)
            if len(data) > MAX_OUTPUT:
                raise ValueError('receipt_ocr_output_limit')
            if await process.wait() != 0:
                raise ValueError('receipt_ocr_failed')
            return data
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def recognize(content):
    from .receipt_ai import document_format
    extension, _ = document_format(content)
    executable = shutil.which('tesseract')
    if not executable:
        raise ValueError('receipt_local_ocr_not_configured')
    with tempfile.TemporaryDirectory(prefix='photoboss-receipt-') as name:
        directory = Path(name)
        source = directory / ('source.' + extension)
        source.write_bytes(content)
        source.chmod(0o600)
        if extension == 'pdf':
            renderer = shutil.which('pdftoppm')
            if not renderer:
                raise ValueError('receipt_pdf_renderer_not_configured')
            await command([renderer, '-f', '1', '-singlefile', '-scale-to', '4000',
                           '-png', str(source), str(directory / 'page')], directory)
            source = directory / 'page.png'
        # Fix phone EXIF orientation and bound the actual OCR raster.
        raster = directory / 'ocr.png'
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert('RGB')
            if image.width * image.height > 20_000_000:
                raise ValueError('receipt_local_ocr_pixel_limit')
            image.thumbnail((4000, 4000))
            image.save(raster, format='PNG')
        raster.chmod(0o600)
        raw = await command([executable, str(raster), 'stdout', '-l', 'rus+eng',
                             '--psm', '6', '--dpi', '150'], directory)
        value = raw.decode('utf-8', errors='strict').strip()
        if not value or len(value) > 24_000:
            raise ValueError('receipt_local_ocr_unreadable')
        return value
