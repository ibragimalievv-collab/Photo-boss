"""Mobile delivery folders keep independent uploads, including identical files."""

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://delivery.example'
BOOKING_ID = 77
PHOTO = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC1kAAAAASUVORK5CYII='
)
SHELL = '''<!doctype html><html lang="ru" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/app/css/styles.css"></head><body>
<script type="module">
import {preferBrowserSession} from '/app/js/api.js';
import {setOutboxUser} from '/app/js/outbox.js';
import {openDelivery} from '/app/js/delivery.js';
preferBrowserSession();setOutboxUser(3);await openDelivery(77);
</script></body></html>'''


async def queued_photos(page):
    return await page.evaluate('''async () => {
      const {openLocal} = await import('/app/js/localstore.js');
      const db = await openLocal();
      try {
        const rows = await new Promise((resolve,reject) => {
          const tx = db.transaction('operations','readonly');
          const request = tx.objectStore('operations').getAll();
          tx.oncomplete = () => resolve(request.result);
          tx.onerror = () => reject(tx.error);
        });
        return await Promise.all(rows.map(async row => ({
          key:row.key,kind:row.kind,status:row.status,data:row.data,
          filename:row.filename,bytes:Array.from(new Uint8Array(await row.blob.arrayBuffer()))
        })));
      } finally {db.close();}
    }''')


async def main():
    errors = []
    media_requests = []
    gallery = {
        'canEdit': True, 'photographer': 'Фотограф съёмки', 'title': 'Независимые папки',
        'published': False, 'expiresAt': None, 'deliveryMode': 'ALL',
        'photos': [
            {'id': 1, 'name': 'all-original.png', 'selected': False, 'uploadSet': 'ALL'},
            {'id': 2, 'name': 'selected-original.png', 'selected': True, 'uploadSet': 'SELECTED'},
            {'id': 3, 'name': 'legacy-selected.png', 'selected': True, 'uploadSet': 'ALL'},
        ],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def intercept(route):
            url = urlsplit(route.request.url)
            if url.netloc != 'delivery.example':
                raise AssertionError(f'Unexpected external request: {route.request.url}')
            if url.path == '/favicon.ico':
                return await route.fulfill(status=204)
            if url.path == '/app/delivery-test':
                return await route.fulfill(body=SHELL, content_type='text/html')
            if url.path.startswith('/app/'):
                f = ROOT / 'app/webapp' / url.path[5:]
                return await route.fulfill(
                    body=f.read_bytes() if f.is_file() else b'',
                    content_type=mimetypes.guess_type(f.name)[0] or 'text/plain',
                )
            if url.path == f'/api/miniapp/delivery/{BOOKING_ID}':
                return await route.fulfill(body=json.dumps(gallery), content_type='application/json')
            if url.path.startswith(f'/api/miniapp/delivery/{BOOKING_ID}/photos/'):
                return await route.fulfill(body=PHOTO, content_type='image/png')
            if url.path == '/api/miniapp/operations/media':
                media_requests.append(route.request.post_data)
            raise AssertionError(f'Unexpected API request: {route.request.url}')

        context = await browser.new_context(viewport={'width': 360, 'height': 844})
        await context.route('**/*', intercept)
        page = await context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.goto(BASE + '/app/delivery-test')
        all_form = page.locator('#deliveryAllUploadForm')
        selected_form = page.locator('#deliverySelectedUploadForm')
        await expect(all_form).to_be_visible()
        await expect(selected_form).to_be_visible()
        for form in (all_form, selected_form):
            await expect(form.locator('input[name="photos"]')).to_have_attribute('multiple', '')
            await expect(form.locator('input[name="photos"]')).to_have_attribute('required', '')

        all_gallery = page.locator('[data-delivery-set="ALL"]')
        selected_gallery = page.locator('[data-delivery-set="SELECTED"]')
        await expect(all_gallery.locator('h3')).to_contain_text('Все фотографии')
        await expect(selected_gallery.locator('h3')).to_contain_text('Выбранные фотографии')
        await expect(all_gallery.locator('[data-photo="1"]')).to_have_count(1)
        await expect(selected_gallery.locator('[data-photo="2"]')).to_have_count(1)
        await expect(selected_gallery.locator('[data-photo="3"]')).to_have_count(1)
        assert await page.locator('[data-delivery="select"]').count() == 0
        assert await page.locator('select[name="target"]').count() == 0
        assert await page.locator('dialog.delivery-sheet').evaluate('el => el.scrollWidth <= el.clientWidth + 2')

        # The same filename and bytes are allowed in each independent folder.
        # Offline queueing avoids all storage-provider calls and real guest messages.
        await context.set_offline(True)
        upload = {'name': 'same-photo.png', 'mimeType': 'image/png', 'buffer': PHOTO}
        await all_form.locator('input[name="photos"]').set_input_files(upload)
        await all_form.get_by_role('button', name='Загрузить все фотографии', exact=True).click()
        await expect(page.locator('[data-delivery-status]')).to_contain_text('сохранено в очередь')
        await expect(all_form.locator('button')).to_be_enabled()
        await selected_form.locator('input[name="photos"]').set_input_files(upload)
        await selected_form.get_by_role('button', name='Загрузить выбранные фотографии', exact=True).click()
        await expect(selected_form.locator('button')).to_be_enabled()
        rows = await queued_photos(page)
        assert len(rows) == 2, rows
        assert {r['data']['photoSet'] for r in rows} == {'ALL', 'SELECTED'}, rows
        assert len({r['key'] for r in rows}) == 2
        for row in rows:
            assert row['kind'] == 'delivery_photo' and row['status'] == 'local', row
            assert row['data']['booking'] == BOOKING_ID, row
            assert row['data']['filename'] == row['filename'] == 'same-photo.png', row
            assert bytes(row['bytes']) == PHOTO
        assert not media_requests, media_requests

        # Read-only employees see both folders without gaining upload controls.
        gallery['canEdit'] = False
        viewer_context = await browser.new_context(viewport={'width': 360, 'height': 844})
        await viewer_context.route('**/*', intercept)
        viewer = await viewer_context.new_page()
        viewer.on('pageerror', lambda e: errors.append(str(e)))
        await viewer.goto(BASE + '/app/delivery-test')
        await expect(viewer.locator('[data-delivery-set="SELECTED"] [data-photo="3"]')).to_have_count(1)
        assert await viewer.locator('#deliveryAllUploadForm, #deliverySelectedUploadForm').count() == 0
        assert await viewer.locator('[data-delivery="select"]').count() == 0
        assert await viewer.locator('dialog.delivery-sheet').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
        assert not errors, errors
        print('PASS: mobile independent ALL/SELECTED uploads of identical files, legacy selected photos, offline queue integrity and read-only gallery; no live uploads or messages')
        await browser.close()


asyncio.run(main())
