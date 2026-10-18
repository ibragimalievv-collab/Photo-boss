"""Prototype disk and gallery actions against real fixture APIs, without live storage."""
import asyncio
import io
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, ImageChops
from playwright.async_api import async_playwright, expect
from test_insights import fixture
from test_miniapp_release import TOKEN, signed

from app.miniapp_api import install_miniapp
from app.models import DeliveryGallery, DeliveryPhoto
from app.services.academy import ACADEMY_BLOCKS, ACADEMY_LESSONS
from app.yandex_disk import ROOT, YandexDisk

BASE = 'https://gallery-navigation.invalid'


async def check_disk_diagnostics(page):
    """Fresh probe, corruption labels, stale quota and navigation during a slow reply."""
    available, read_ok = True, True
    entered, release = asyncio.Event(), asyncio.Event()
    hold = False
    async def account(route):
        if hold:
            entered.set()
            await release.wait()
        await route.fulfill(json={'configured': True, 'available': available,
            **({'account': {'login': 'fixture-disk'}, 'used': 50, 'total': 100, 'root': 'PhotoBoss'} if available else {})})
    async def probe(route):
        await route.fulfill(json={'connected': True, 'writeVerified': True, 'readVerified': read_ok,
            'checkedAt': '2026-10-18T13:00:00Z', 'error': None if read_ok else 'Чтение не подтверждено'})
    async def inventory(route):
        await route.fulfill(json={'note': 'Проверены последние файлы.', 'items': [
            {'source': 'original', 'id': 1, 'at': '2026-10-18T13:00:00Z', 'diskStatus': 'mismatch'},
            {'source': 'gallery', 'id': 2, 'at': '2026-10-18T13:00:00Z', 'diskStatus': 'verified'}]})
    routes = [('**/api/miniapp/galleries/storage*', account),
              ('**/api/miniapp/storage/probe', probe), ('**/api/miniapp/storage/inventory', inventory)]
    for pattern, handler in routes:
        await page.route(pattern, handler)
    try:
        for width in [1440, 390]:
            await page.set_viewport_size({'width': width, 'height': 1000 if width == 1440 else 844})
            await page.locator('[data-disk-info]').click()
            await expect(page.locator('.modal')).to_contain_text('fixture-disk')
            await page.locator('[data-disk-refresh]').click()
            await expect(page.locator('[data-disk-inventory]')).to_contain_text('Запись и чтение подтверждены:')
            await expect(page.locator('[data-disk-inventory]')).to_contain_text('контрольная сумма не совпадает')
            assert await page.locator('.modal').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            await page.locator('[data-modal-close]').click()
        available = False
        await page.locator('[data-disk-info]').click()
        await expect(page.locator('.modal')).to_contain_text('Не удалось получить сведения')
        await expect(page.locator('.storage small')).not_to_contain_text('50%')
        read_ok = False
        await page.locator('[data-disk-refresh]').click()
        await expect(page.locator('[data-disk-inventory]')).to_have_text('Чтение не подтверждено')
        await expect(page.locator('[data-disk-inventory]')).not_to_contain_text('№2')
        await page.locator('[data-modal-close]').click()
        # A late account response must not reopen a dialog after leaving this view.
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        available, hold = True, True
        await page.locator('[data-disk-info]').click()
        await asyncio.wait_for(entered.wait(), 5)
        await page.locator('#galleryWorkspace [data-go=home]').first.click()
        await expect(page.locator('#sidebar')).to_be_visible()
        release.set()
        await page.wait_for_load_state('networkidle')
        assert await page.locator('dialog.modal:visible').count() == 0
    finally:
        release.set()
        for pattern, handler in routes:
            await page.unroute(pattern, handler)
    await page.goto(BASE + '/app/#galleries')
    await expect(page.locator('#app h1')).to_have_text('Диск и клиентские галереи')
    await page.set_viewport_size({'width': 1440, 'height': 1000})


async def wait_images(page, selector):
    for _ in range(100):
        if await page.locator(selector).evaluate_all('images => images.every(i=>i.complete&&i.naturalWidth>0)'):
            return
        await asyncio.sleep(.05)
    raise AssertionError(await page.locator(selector).evaluate_all('images => images.map(i=>({src:i.src,complete:i.complete,width:i.naturalWidth}))'))


async def compare_disk(context, page, output):
    reference = await context.new_page()
    await reference.goto(BASE + '/__approved_disk')
    data = await page.evaluate("""() => ({name:document.querySelector('.account b').textContent,
        nav:[...document.querySelectorAll('.side .menu button')].map(b=>b.textContent),
        row:document.querySelector('.table .row').innerHTML,
        card:document.querySelector('.cards .card').innerHTML})""")
    await reference.evaluate("""data => {
        document.querySelector('.account b').textContent=data.name;
        [...document.querySelectorAll('.side .menu button')].forEach((b,i)=>b.textContent=data.nav[i]);
        document.querySelector('#rows').innerHTML='<div class="row">'+data.row+'</div>';
        document.querySelector('#cards').innerHTML='<div class="card">'+data.card+'</div>';
        document.querySelectorAll('.project, .card>button:not(.round), .link').forEach(b=>{
            const div=document.createElement('div');div.className=b.className;div.innerHTML=b.innerHTML;b.replaceWith(div);
        });
        document.querySelectorAll('.pname,.project .meta').forEach(el=>el.style.display='block');
    }""", data)
    measure = """selectors => Object.fromEntries(selectors.map(selector=>{
        const el=document.querySelector(selector),s=getComputedStyle(el),r=el.getBoundingClientRect();
        return [selector,{x:r.x,y:r.y,width:r.width,height:r.height,font:s.fontFamily,fontSize:s.fontSize,
        fontWeight:s.fontWeight,color:s.color,background:s.backgroundColor}];
    }))"""
    differences = []
    for width,height in [(1440,1000),(390,844)]:
        await page.set_viewport_size({'width':width,'height':height})
        await reference.set_viewport_size({'width':width,'height':height})
        selectors=['.wrap','.title','.title h1','.storage','.tabs','.toolbar','.search']
        selectors += ['.side','.logo','.account','.menu','.top','.table','.thead','.row','.project','.thumb'] if width>850 else ['.mobile','.cards','.card']
        actual, expected = await page.evaluate(measure,selectors), await reference.evaluate(measure,selectors)
        for selector in selectors:
            for key,value in expected[selector].items():
                got=actual[selector][key]
                if not (abs(got-value)<=1 if isinstance(value,(int,float)) else got==value):
                    differences.append((width,selector,key,got,value))
        await reference.screenshot(path=str(output / f'prototype-disk-{width}.png'),full_page=True)
    await reference.close()
    await page.set_viewport_size({'width':1440,'height':1000})
    assert not differences,differences


async def compare_prototype(context, guest, output):
    reference = await context.new_page()
    await reference.goto(BASE + '/__approved_gallery')
    content = await guest.evaluate("""() => ({title:document.querySelector('.hero h1').textContent,
        meta:document.querySelector('.herotxt>div').textContent, cover:document.querySelector('.hero img').src,
        photos:[...document.querySelectorAll('.photo')].map(p=>({url:p.querySelector('img').src,
        favorite:p.querySelector('.heart').classList.contains('on')}))})""")
    await reference.evaluate("""data => {
        document.querySelector('#gtitle').textContent=data.title;
        document.querySelector('#gmeta').textContent=data.meta;
        document.querySelector('#cover').src=data.cover;
        document.querySelector('#favCount').textContent=data.photos.filter(p=>p.favorite).length;
        document.querySelector('#grid').innerHTML=data.photos.map(p=>`<div class="photo"><img src="${p.url}"><button class="round heart ${p.favorite?'on':''}">${p.favorite?'♥':'♡'}</button></div>`).join('');
    }""", content)
    selectors = ['.gallery', '.gbar', '.gbrand', '.gbutton', '.hero', '.hero>img', '.herotxt', '.herotxt h1', '.gallerymain',
                 '.ghead', '.ghead h2', '.ghead p', '.pills', '.pill', '.pill:last-child', '.grid', '.photo', '.photo img', '.round']
    measure = """selectors => Object.fromEntries(selectors.map(selector=>{
        const el=document.querySelector(selector),s=getComputedStyle(el),r=el.getBoundingClientRect();
        return [selector,{x:r.x,y:r.y,width:r.width,height:r.height,font:s.fontFamily,fontSize:s.fontSize,
        fontWeight:s.fontWeight,color:s.color,background:s.backgroundColor}];
    }))"""
    differences = []
    for width, height in [(390, 844), (1440, 1000)]:
        await guest.set_viewport_size({'width': width, 'height': height})
        await reference.set_viewport_size({'width': width, 'height': height})
        for page in [guest, reference]:
            await page.locator('.photo img').scroll_into_view_if_needed()
            await wait_images(page,'.photo img,.hero img')
            await page.evaluate('scrollTo(0,0)')
        actual, expected = await guest.evaluate(measure, selectors), await reference.evaluate(measure, selectors)
        for selector in selectors:
            for key, value in expected[selector].items():
                got = actual[selector][key]
                if not (abs(got-value) <= 1 if isinstance(value, (int, float)) else got == value):
                    differences.append((width, selector, key, got, value))
        await guest.screenshot(path=str(output / f'guest-matched-{width}.png'), full_page=True)
        await reference.screenshot(path=str(output / f'prototype-{width}.png'), full_page=True)
        with Image.open(output / f'guest-matched-{width}.png') as actual_image, Image.open(output / f'prototype-{width}.png') as expected_image:
            assert actual_image.size == expected_image.size
            assert ImageChops.difference(actual_image.convert('RGB'), expected_image.convert('RGB')).getbbox() is None, width
    await reference.close()
    assert not differences, differences
    await guest.set_viewport_size({'width': 390, 'height': 844})


async def main():
    engine, factory, _ = await fixture()
    bot = SimpleNamespace(token=TOKEN, send_message=AsyncMock(),
                          me=AsyncMock(return_value=SimpleNamespace(username='fixture_bot')))
    app = web.Application()
    api = install_miniapp(app, engine=engine, bot=bot, lessons=ACADEMY_LESSONS, blocks=ACADEMY_BLOCKS)
    api.today = lambda: date(2026, 9, 21)
    original = api.rows
    async def rows(conn, sql, **params):
        return await original(conn, sql.replace(' FOR UPDATE', ''), **params)
    api.rows = rows
    storage = {}
    async with factory() as session:
        g = DeliveryGallery(booking_id=1, title='Movenpick 16:00', published=True, delivery_mode='ALL',
                            created_by_id=1, access_token='g'*32)
        session.add(g)
        await session.flush()
        for i in range(1, 7):
            sample = Path(os.getenv('GALLERY_QA_PHOTOS', '/nonexistent')) / f'photo-{i:02}.jpg'
            if sample.is_file():
                raw = sample.read_bytes()
            else:
                stream = io.BytesIO()
                Image.new('RGB', (500, 700) if i % 2 else (700, 500), ['#617460', '#8c8674', '#c5b7a2'][i % 3]).save(stream, 'JPEG')
                raw = stream.getvalue()
            storage[ROOT + f'/delivery/booking-1/test/{i}.jpg'] = raw
            session.add(DeliveryPhoto(id=i, gallery_id=g.id, uploaded_by_id=1, filename=f'family-{i}.jpg',
                disk_path=ROOT + f'/delivery/booking-1/test/{i}.jpg', sha256=str(i)*64, byte_size=len(raw), selected=i == 2, upload_set='ALL'))
        await session.commit()
    delivery = app['delivery']
    async def photo_bytes(photo):
        return storage[photo.disk_path]
    delivery.bytes = photo_bytes
    client = TestClient(TestServer(app))
    await client.start_server()
    errors = []
    output = Path(os.getenv('GALLERY_QA_DIR', '/tmp/photo-boss-gallery-qa'))
    output.mkdir(parents=True, exist_ok=True)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            init = signed(1)
            await context.add_init_script('window.Telegram={WebApp:{initData:' + json.dumps(init) + ',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};')
            async def route(r):
                if r.request.url.startswith('https://telegram.org/'):
                    return await r.fulfill(body='', content_type='text/javascript')
                assert r.request.url.startswith(BASE), r.request.url
                if r.request.url in {BASE + '/__approved_gallery', BASE + '/__approved_disk'}:
                    name='approved_disk' if r.request.url.endswith('_disk') else 'approved_gallery'
                    return await r.fulfill(body=Path(f'tests/fixtures/{name}.html').read_text(), content_type='text/html')
                response = await client.request(r.request.method, r.request.url.removeprefix(BASE),
                    data=r.request.post_data_buffer, headers={'X-Telegram-Init-Data': init,
                    'Content-Type': r.request.headers.get('content-type', 'application/json')})
                await r.fulfill(status=response.status, body=await response.read(),
                                headers={k: v for k, v in response.headers.items() if k.lower() in {'content-type', 'content-security-policy', 'cache-control'}})
            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(BASE + '/app/#galleries')
            await expect(page.locator('#app h1')).to_have_text('Диск и клиентские галереи')
            await expect(page.locator('.table [data-gp="open"]')).to_have_count(1)
            await page.locator('[data-project-search]').fill('missing')
            await expect(page.locator('.table .empty')).to_be_visible()
            await page.locator('[data-project-search]').fill('Movenpick')
            await expect(page.locator('.table [data-gp="open"]')).to_have_count(1)
            await wait_images(page,'.table .thumb')
            await compare_disk(context,page,output)
            await check_disk_diagnostics(page)
            await page.screenshot(path=str(output / 'disk-desktop.png'), full_page=True)
            await page.locator('.table [data-gp="favorite"]').click()
            await expect(page.locator('.table [data-gp="favorite"]')).to_have_attribute('aria-pressed', 'true')
            await page.reload()
            await expect(page.locator('.table [data-gp="favorite"]')).to_have_attribute('aria-pressed', 'true')
            await page.locator('.table [data-gp="open"]').click()
            await expect(page.locator('.hero h1')).to_have_text('Movenpick 16:00')
            await expect(page.locator('.grid .photo')).to_have_count(6)
            await page.locator('[data-gv-filter=selected]').click()
            await expect(page.locator('.grid .photo')).to_have_count(1)
            await expect(page.locator('[data-gv-photo="2"]')).to_be_visible()
            await page.locator('[data-gv-filter=all]').click()
            await expect(page.locator('.grid .photo')).to_have_count(6)
            await expect(page.locator('.hero img')).to_have_attribute('src', __import__('re').compile(r'^blob:'))
            await page.screenshot(path=str(output / 'gallery-desktop.png'), full_page=True)
            await page.locator('[data-gv-favorite="2"]').click()
            await expect(page.locator('[data-fav-count]')).to_have_text('1')
            await page.locator('[data-gv-filter="fav"]').click()
            await expect(page.locator('.grid .photo')).to_have_count(1)
            await page.locator('[data-gv-photo="2"]').click()
            await expect(page.locator('dialog.viewer')).to_be_visible()
            with patch('app.delivery.configured_from_env', return_value=('fake', 'fake')), patch.object(YandexDisk, 'ensure_dir', new=AsyncMock()), patch.object(YandexDisk, 'upload_bytes', new=AsyncMock()), patch.object(YandexDisk, 'delete', new=AsyncMock()):
                await page.locator('[data-v=selected]').click()
                await expect(page.locator('[data-v=selected]')).to_have_text('Выбрать для клиента')
                await page.locator('[data-v=selected]').click()
                await expect(page.locator('[data-v=selected]')).to_have_text('Убрать из выбранных')
            await page.locator('[data-v="cover"]').click()
            await expect(page.locator('.hero img')).to_have_attribute('data-gallery-image', '2')
            async with page.expect_download() as download:
                await page.locator('[data-v="download"]').click()
            assert (await download.value).suggested_filename == 'family-2.jpg'
            await page.locator('[data-v="close"]').click()
            await page.locator('[data-gv="tools"]').click()
            await page.locator('[data-gv-menu="settings"]').click()
            await page.locator('.modal [name=title]').fill('Семья в Анапе')
            await page.locator('.modal [name=days]').select_option('30')
            await page.locator('.modal [type=submit]').click()
            await expect(page.locator('.hero h1')).to_have_text('Семья в Анапе')
            await page.locator('[data-gv=share]').click()
            await expect(page.locator('.gallery-qr')).to_have_attribute('src', __import__('re').compile(r'^blob:'))
            await page.screenshot(path=str(output / 'share-qr.png'), full_page=True)
            async with page.expect_download() as download:
                await page.locator('[data-share=qr]').click()
            assert (await download.value).suggested_filename == 'Photo-Boss-1-QR.png'
            await page.locator('.modal [data-modal-close]').click()
            await page.locator('[data-gv="back"]').click()
            await expect(page.locator('#app h1')).to_have_text('Диск и клиентские галереи')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await expect(page.locator('.cards .card')).to_be_visible()
            assert await page.locator('#app').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            await page.screenshot(path=str(output / 'disk-mobile.png'), full_page=True)
            await page.locator('.cards [data-gp="menu"]').click()
            await page.locator('[data-menu="trash"]').click()
            await expect(page.locator('.cards .card')).to_have_count(0)
            await page.locator('[data-gp-mode="trash"]').click()
            await expect(page.locator('.cards .card')).to_have_count(1)
            assert (await client.get('/g/' + 'g'*32)).status == 404
            await page.locator('.cards [data-gp="menu"]').click()
            await page.locator('[data-menu="trash"]').click()
            await page.locator('[data-gp-mode="all"]').click()
            await page.locator('.cards [data-gp="open"]').click()
            await expect(page.locator('.hero h1')).to_have_text('Семья в Анапе')
            await page.locator('[data-gv=tools]').click()
            await expect(page.locator('[data-gv-menu=upload]')).to_be_visible()
            await page.locator('[data-gv-menu=close]').click()
            for photo in await page.locator('.grid .photo img').all():
                await photo.scroll_into_view_if_needed()
                await expect(photo).to_have_js_property('complete', True)
            await wait_images(page,'.grid .photo img,.hero img')
            await page.evaluate('scrollTo(0,0)')
            await page.screenshot(path=str(output / 'gallery-mobile.png'), full_page=True)
            assert await page.locator('#app').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            # Guest uses the identical shared layout and persisted favorites, never the staff shell.
            guest = await context.new_page()
            await guest.set_viewport_size({'width': 390, 'height': 844})
            guest.on('pageerror', lambda e: errors.append(str(e)))
            await guest.goto(BASE + '/g/' + 'g'*32)
            await expect(guest.locator('.hero h1')).to_have_text('Семья в Анапе')
            await expect(guest.locator('[data-fav-count]')).to_have_text('1')
            await expect(guest.locator('.grid .photo')).to_have_count(1)
            assert await guest.locator('[data-gv="tools"], .project-actions').count() == 0
            await guest.locator('[data-gv-filter="fav"]').click()
            await expect(guest.locator('.grid .photo')).to_have_count(1)
            await guest.locator('[data-gv-filter=all]').click()
            await compare_prototype(context, guest, output)
            await guest.screenshot(path=str(output / 'guest-mobile.png'), full_page=True)
            await guest.locator('[data-gv=share]').click()
            await expect(guest.locator('.gallery-actions-menu')).to_be_visible()
            async with guest.expect_download() as download:
                await guest.locator('[data-gv-menu=download-all]').click()
            assert (await download.value).suggested_filename.endswith('.jpg')
            await guest.locator('[data-gv-menu=close]').click()
            await guest.locator('[data-gv-photo]').click()
            await expect(guest.locator('.viewer')).to_be_visible()
            assert await guest.locator('.viewer button').count() == 4
            await guest.locator('[data-v=close]').click()
            await page.locator('[data-gv=back]').click()
            await page.set_viewport_size({'width': 1440, 'height': 1000})
            await expect(page.locator('#galleryWorkspace .side')).to_be_visible()
            await page.locator('#galleryWorkspace [data-go=home]').first.click()
            await expect(page.locator('#sidebar')).to_be_visible()
            assert not await page.locator('body').evaluate("el => el.classList.contains('gallery-mode')")
            await page.goto(BASE + '/app/#galleries')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.locator('.cards [data-gp=open]').click()
            await page.locator('[data-gv="back"]').click()
            await page.locator('.cards [data-gp="menu"]').click()
            await page.locator('[data-menu="trash"]').click()
            await page.locator('[data-gp-mode="trash"]').click()
            await page.locator('[data-gp="purge"]').click()
            await expect(page.locator('.modal')).to_contain_text('Восстановить их будет невозможно')
            await page.locator('.modal [name="confirm"]').check()
            with patch('app.yandex_disk.configured_from_env', return_value=('fake', 'fake')), patch.object(YandexDisk, 'delete_confirmed', new=AsyncMock()) as removed, patch.object(YandexDisk, '_json', new=AsyncMock(return_value={'total_space': 1000, 'used_space': 0})):
                await page.locator('.modal [type=submit]').click()
                await expect(page.locator('.cards .card')).to_have_count(0)
                removed.assert_awaited_with(ROOT + '/delivery/booking-1')
            assert (await client.get('/g/' + 'g'*32)).status == 404
            assert not errors, errors
            assert not bot.send_message.called
            await browser.close()
            print('PASS: prototype disk/gallery, desktop/mobile, search, saved favorites, original download, cover, settings, trash/restore, confirmed purge, selected-only QR downloads and shared guest layout')
    finally:
        await client.close()
        await engine.dispose()


asyncio.run(main())
