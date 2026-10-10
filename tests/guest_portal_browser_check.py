"""Mobile HTTPS guest path, real MP4 and fake bot/storage; no external traffic."""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from test_guest_portal import setup, signed

from app import family_video


async def main():
    with tempfile.TemporaryDirectory(prefix='pb-guest-qa-') as directory:
        root = Path(directory)
        await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(root/'key.pem'), '-out', str(root/'cert.pem'), '-days', '1', '-subj', '/CN=localhost'],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root/'cert.pem', root/'key.pem')
        monkeypatch = pytest.MonkeyPatch()
        engine, service, client, files = await setup(monkeypatch, ssl_context=tls)
        origin = str(client.make_url('/')).rstrip('/')
        monkeypatch.setattr(service.delivery, 'origin', lambda: origin)
        try:
            async with async_playwright() as playwright:
                binary = os.getenv('GUEST_QA_BROWSER')
                skip_decode = os.getenv('GUEST_QA_SKIP_DECODE') == '1'
                browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'],
                    **({'executable_path': binary} if binary else {} if skip_decode else {'channel': 'chrome'}))
                context = await browser.new_context(ignore_https_errors=True, viewport={'width': 390, 'height': 844})
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await context.route('https://telegram.org/**', lambda route: route.abort())
                await page.add_init_script("window.Telegram={WebApp:{initData:"+repr(signed(100001))+",ready(){},expand(){}}}")
                await page.goto(origin+'/guest/')
                await page.get_by_role('heading', name='Ваши фотографии, Guest one').wait_for()
                await page.get_by_role('button', name='Album 1', exact=True).click()
                await page.locator('input[name=photo][value="1"]').check()
                await page.locator('input[name=photo][value="2"]').check()
                await page.locator('input[name=names]').fill('Анна, Михаил, дети')
                await page.locator('textarea[name=story]').fill('Наш семейный отпуск')
                await page.get_by_role('button', name='Подготовить слайд-шоу', exact=True).click()
                await page.locator('#renderFilm').wait_for()
                await family_video.command('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', str(root/'music.wav'))
                await page.locator('input[name=audio]').set_input_files(root/'music.wav')
                await page.locator('input[name=confirmed]').check()
                await page.get_by_role('button', name='Создать MP4', exact=True).click()
                await page.get_by_text('Ролик в очереди.', exact=False).wait_for()
                assert await service.run_once()
                await page.get_by_role('link', name='Скачать MP4', exact=True).wait_for(timeout=15000)
                href = await page.get_by_role('link', name='Скачать MP4', exact=True).get_attribute('href')
                download = await page.request.get(origin+href)
                assert download.status == 200
                movie = root/'download.mp4'
                movie.write_bytes(await download.body())
                info = json.loads(await family_video.command('ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(movie)))
                assert any(s.get('codec_name') == 'h264' for s in info['streams'])
                assert any(s.get('codec_name') == 'aac' for s in info['streams'])
                assert 5.9 <= float(info['format']['duration']) <= 6.1
                if not skip_decode:
                    await page.locator('video').evaluate('''v => new Promise((resolve, reject) => {
                        if (v.readyState >= 1) return resolve();
                        const timer = setTimeout(() => reject(Error('Video metadata timeout')), 15000);
                        v.addEventListener('loadedmetadata', () => {clearTimeout(timer); resolve();}, {once:true});
                        v.addEventListener('error', () => {clearTimeout(timer); reject(Error('Video decode failed: '+v.error?.code+' '+v.error?.message));}, {once:true});
                        v.load();
                    })''')
                    assert 5.9 <= await page.locator('video').evaluate('(v)=>v.duration') <= 6.1
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                output = Path(os.getenv('GUEST_QA_DIR', '/tmp/photo-boss-guest-qa'))
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=output/'guest-mobile.png', full_page=True)
                await page.set_viewport_size({'width': 1440, 'height': 1000})
                await page.screenshot(path=output/'guest-desktop.png', full_page=True)
                await page.locator('#password input[name=password]').fill('guest-long-password-123')
                await page.locator('#password input[name=repeat]').fill('guest-long-password-123')
                await page.get_by_role('button', name='Сохранить пароль', exact=True).click()
                await page.get_by_text('Пароль сохранён.', exact=False).wait_for()
                await page.get_by_role('button', name='Выйти', exact=True).click()
                await page.locator('#login input[name=login]').fill('100001')
                await page.locator('#login input[name=password]').fill('guest-long-password-123')
                await page.locator('#login button').click()
                await page.get_by_role('heading', name='Ваши фотографии, Guest one').wait_for()
                assert not errors, errors
                assert any(path.endswith('.mp4') for path in files)
                await browser.close()
                print('Guest mobile/desktop HTTPS, MP4 download with audio, password login and no overflow: passed')
                print('Browser MP4 decoder: '+('not checked in this runtime; strict Chrome check remains in CI' if skip_decode else 'passed'))
        finally:
            await client.close()
            await engine.dispose()
            monkeypatch.undo()


if __name__ == '__main__':
    asyncio.run(main())
