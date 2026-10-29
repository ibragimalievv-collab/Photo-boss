"""Real workflow UI/IndexedDB with a strict synthetic HTTP ledger, never production.

Exercise three selected files, third-file failure/lost acknowledgement, a second
independent sale, reload/resume, final gallery/payment readiness and manager QR.
Backend storage/lineage is verified separately by the PostgreSQL service tests.
"""
import asyncio
import hashlib
import io
import json
import os
import re
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
API = """
export async function api(path,options={}){
 const {body,method='GET',responseType}=options;
 const response=await fetch('/fixture'+path,{method,body:body instanceof FormData?body:body?JSON.stringify(body):undefined,
  headers:body&&!(body instanceof FormData)?{'Content-Type':'application/json'}:{}});
 if(!response.ok){const data=await response.json();const error=new Error(data.error||'Synthetic failure');error.status=response.status;throw error;}
 return responseType==='blob'?response.blob():response.json();
}
"""
SHELL = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<link rel=stylesheet href=/app/css/styles.css><main id=app></main><p id=errors role=alert></p>
<script type=module>
import * as workflow from '/app/js/workflow.js';
import {setOutboxUser} from '/app/js/outbox.js';
import {setFormActor,restoreForms} from '/app/js/form-state.js';
window.workflow=workflow;
setFormActor(42);await setOutboxUser(42);await workflow.loadWorkflow();
document.querySelector('#app').innerHTML=workflow.workflowView();restoreForms();
document.addEventListener('change',event=>{if(event.target.id==='workflowSalePick')workflow.workflowSelect(event.target.value);});
document.addEventListener('submit',async event=>{
 if(event.target.id!=='workflowSale')return;event.preventDefault();
 const form=event.target,button=form.querySelector('[type=submit]');button.disabled=true;document.querySelector('#errors').textContent='';
 try{await workflow.workflowSubmit(form);}catch(error){document.querySelector('#errors').textContent=error.message;}
 finally{button.disabled=false;form.dispatchEvent(new CustomEvent('pb-workflow-submit-finished',{bubbles:true}));}
});
document.addEventListener('click',async event=>{const button=event.target.closest('[data-workflow]');if(!button)return;
 try{await workflow.workflowClick(button);}catch(error){document.querySelector('#errors').textContent=error.message;}
});
window.dispatchEvent(new Event('pb-workflow-mounted'));
</script>"""


def png(color):
    out = io.BytesIO()
    Image.new('RGB', (24, 20), color).save(out, format='PNG')
    return out.getvalue()


async def scenario(browser, viewport, label, output):
    ledger, drafts, sales, calls, attempts = {}, {}, {}, [], {}
    paid, unverified = set(), set()
    contexts = {bid: 'Гость с одинаковым именем' for bid in (7, 8, 9)}
    faults = {'temporary': False, 'lost': False, 'inline_rejected': False}
    inline_price = {'value': '400.00'}

    def gallery(bid):
        sale = sales.get(bid)
        ready = bool(sale and bid in paid and bid not in unverified)
        return {'saleId': sale['id'] if sale else None, 'ready': ready,
                'state': 'storage_pending' if bid in unverified else 'ready' if ready else 'payment_pending',
                'clientUrl': f'http://127.0.0.1/g/{str(bid).zfill(32)}' if ready else None,
                'qrPath': f'/sales/{sale["id"]}/gallery-qr' if ready else None,
                'syncPath': f'/sales/{sale["id"]}/gallery/sync' if sale else None,
                'photoCount': len(drafts.get(bid, {}).get('photos', {}))}

    def listing():
        rows = []
        for bid, client in contexts.items():
            draft, sale = drafts.get(bid), sales.get(bid)
            rows.append({'id': bid, 'client': client, 'hotel': 'Тестовый отель', 'hotelId': 1,
                'date': '2026-10-28', 'time': '12:00', 'room': str(bid),
                'photographer': 'Фотограф съёмки', 'status': 'SOLD' if sale else 'READY_FOR_SALE',
                'price': inline_price['value'] if bid >= 10 else '400.00', 'totalFrames': 0 if bid >= 10 else 3, 'uploadedOriginals': 0 if bid >= 10 else 3, 'fullUploaded': bid < 10,
                'pricing': {'kind': 'UNIT', 'price': '400.00', 'tariffId': 1},
                'canUpload': False, 'files': [], 'receipts': [], 'bankPayments': [],
                'sales': [{'id': sale['id'], 'photos': sale['sold'], 'amount': sale['amount']}] if sale else [],
                'soldTotal': sale['sold'] if sale else 0, 'salesAmount': sale['amount'] if sale else '0',
                'paidAmount': sale['amount'] if bid in paid and sale else '0', 'gallery': gallery(bid),
                'draft': {'id': draft['id'], 'mine': True, 'status': draft['status'],
                    'selected': len(draft['photos']), 'sold': draft.get('sold'), 'total': draft.get('total'),
                    'receiptStored': draft.get('receipt', False)} if draft and not sale else None})
        return {'actorId': 42, 'date': '2026-10-29', 'bookings': rows, 'hotels': [{'id': 1, 'name': 'Тестовый отель', 'unitPrice': inline_price['value']}],
                'inlineSale': {'hotelIds': [1], 'defaultHotelId': 1, 'defaultPhotographerId': None, 'photographerLocked': False, 'time': '12:00', 'photographers': [{'id': 2, 'name': 'Настоящий фотограф', 'hotelIds': [1]}]},
                'packages': [{'id': 1, 'name': 'Согласованная цена', 'price': inline_price['value']}], 'tariffs': [], 'albums': [], 'bankConfigured': False, 'canBook': False}

    async def operation(request):
        filename = None
        if request.content_type.startswith('multipart/'):
            reader = await request.multipart()
            op = json.loads(await (await reader.next()).text())
            part = await reader.next()
            filename = part.filename
            original = bytes(await part.read())
        else:
            op, original = await request.json(), None
        key, kind, data = op['key'], op['kind'], op['data']
        calls.append({'key': key, 'kind': kind, 'filename': filename})
        digest = hashlib.sha256(json.dumps(op, sort_keys=True).encode() + (original or b'')).hexdigest()
        if key in ledger:
            assert ledger[key][0] == digest, 'Changed payload under original key'
            return web.json_response(ledger[key][1])
        if kind in {'sale_start', 'sale_start_inline'}:
            if kind == 'sale_start_inline':
                assert data['guest']['photographerId'] == 2
                if not faults['inline_rejected']:
                    faults['inline_rejected'] = True
                    inline_price['value'] = '500.00'
                    return web.json_response({'error': 'Цена пакета изменилась. Обновите данные и проверьте продажу.'}, status=409)
                assert data['price'] == '500.00', 'Correction must use the current server quote'
                bid = 10
                contexts[bid] = data['guest']['name']
            else:
                bid = data['booking']['id']
            drafts.setdefault(bid, {'id': 100+bid, 'photos': {}, 'status': 'AWAITING_RECEIPT'})
            result = {'draftId': drafts[bid]['id'], 'bookingId': bid}
        else:
            start = ledger[data['draft']['key']][1]
            bid, draft = start['bookingId'], drafts[start['bookingId']]
            result = {'draftId': draft['id'], 'bookingId': bid}
            if kind == 'sale_receipt':
                assert original
                draft['receipt'], draft['status'] = True, 'AWAITING_COUNTS'
            elif kind == 'sale_counts':
                assert draft.get('receipt'), 'Counts bypassed receipt'
                draft.update(total=data['total'], sold=data['sold'], status='AWAITING_SELECTED')
            elif kind == 'sale_selected':
                assert draft.get('sold'), 'File bypassed counts'
                attempts[key] = attempts.get(key, 0)+1
                if bid == 9 and filename == 'paused.png' and attempts[key] <= 4:
                    return web.json_response({'error': 'Синтетический повторяющийся сбой файла'}, status=503)
                if bid == 7 and filename == 'three.png' and attempts[key] == 1:
                    faults['temporary'] = True
                    return web.json_response({'error': 'Синтетический временный сбой одного файла'}, status=503)
                photo = hashlib.sha256(original).hexdigest()
                draft['photos'].setdefault(photo, 1000+len(draft['photos']))
                result |= {'photoId': draft['photos'][photo], 'selectedPhotos': len(draft['photos'])}
            elif kind == 'sale_complete':
                assert len(draft['photos']) == draft['sold'], 'Completion bypassed a failed file'
                sales.setdefault(bid, {'id': 200+bid, 'sold': draft['sold'], 'amount': str((500 if bid >= 10 else 400)*draft['sold'])})
                draft['status'] = 'COMPLETED'
                result |= {'saleId': sales[bid]['id'], 'soldPhotos': sales[bid]['sold'],
                           'amount': sales[bid]['amount'], 'gallery': gallery(bid)}
        ledger[key] = (digest, result)
        if kind == 'sale_selected' and bid == 7 and filename == 'three.png' and not faults['lost']:
            faults['lost'] = True
            request.transport.close()  # accepted by server, acknowledgement lost
        return web.json_response(result)

    async def files(request):
        path = request.match_info['path']
        if path == 'js/api.js':
            return web.Response(text=API, content_type='text/javascript')
        target = (ROOT/'app/webapp'/path).resolve()
        if not target.is_relative_to(ROOT/'app/webapp') or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def sync_gallery(request):
        sale_id = int(request.match_info['sale'])
        bid = next(bid for bid, sale in sales.items() if sale['id'] == sale_id)
        unverified.discard(bid)
        return web.json_response({'gallery': gallery(bid)})

    app = web.Application(client_max_size=25*1024*1024)
    app.router.add_get('/', lambda request: web.Response(text=SHELL, content_type='text/html'))
    app.router.add_get('/app/{path:.*}', files)
    app.router.add_get('/fixture/workflow', lambda request: web.json_response(listing()))
    app.router.add_post('/fixture/operations/{type}', operation)
    app.router.add_get('/fixture/sales/{sale}/gallery-qr', lambda request: web.Response(body=png('black'), content_type='image/png'))
    app.router.add_post('/fixture/sales/{sale}/gallery/sync', sync_gallery)
    server = TestServer(app)
    await server.start_server()
    context = await browser.new_context(viewport=viewport)
    page = await context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        await page.clock.install()
        await page.goto(str(server.make_url('/')))
        await page.locator('#workflowSalePick').select_option('7')
        form = page.locator('#workflowSale')
        await form.locator('[name=selected]').set_input_files({'name': 'invalid.pdf', 'mimeType': 'application/pdf', 'buffer': b'not a photo'})
        await form.get_by_role('button', name='Сохранить продажу', exact=True).click()
        await expect(page.locator('#errors')).to_contain_text('JPEG, PNG или WebP')
        assert not calls
        assert await page.evaluate("async()=> (await (await import('/app/js/outbox.js')).outboxRows()).length") == 0
        await expect(form.get_by_role('button', name='Сохранить продажу', exact=True)).to_be_enabled()
        await form.locator('[name=selected]').set_input_files([
            {'name': name, 'mimeType': 'image/png', 'buffer': png(color)}
            for name, color in [('one.png', 'red'), ('two.png', 'green'), ('three.png', 'blue')]])
        await expect(form.locator('[name=sold]')).to_have_value('3')
        await expect(form.locator('[name=total]')).to_have_value('3')
        await expect(form.locator('[data-operation-progress]')).to_contain_text('Файлы ещё не переданы')
        await form.locator('[name=receipt]').set_input_files({'name': 'receipt.png', 'mimeType': 'image/png', 'buffer': png('white')})
        await form.screenshot(path=str(output/f'{label}-minimal-existing.png'))
        await form.evaluate("form=>{for(let n=0;n<2;n++)form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));}")
        await expect(form.locator('[data-operation-progress]')).to_contain_text('Синтетический временный сбой', timeout=15000)
        first_keys = await form.get_attribute('data-pending-keys')
        assert len(drafts[7]['photos']) == 2 and 7 not in sales
        assert sum(kind['kind'] == 'sale_start' for kind in calls) == 1
        await expect(form.locator('[data-sale-gallery]')).to_have_count(0)
        await form.locator('[data-workflow=another-sale]').click()
        await page.locator('#workflowSalePick').select_option('8')
        await expect(form).not_to_have_attribute('data-pending-keys', first_keys)
        await form.locator('[name=selected]').set_input_files({'name': 'other.png', 'mimeType': 'image/png', 'buffer': png('orange')})
        await form.locator('[name=receipt]').set_input_files({'name': 'receipt-2.png', 'mimeType': 'image/png', 'buffer': png('gray')})
        await form.get_by_role('button', name='Сохранить продажу', exact=True).click()
        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=15000)
        assert 8 in sales and 7 not in sales, 'Second sale was blocked by the first file'
        await expect(form.locator('[data-sale-client-link]')).to_have_count(0)
        paid.add(8)
        await page.clock.fast_forward(6000)
        await expect(form.locator('[data-sale-client-link]')).to_be_visible()
        await form.locator('[data-workflow=gallery-qr]').click()
        await expect(form.locator('.sale-gallery-qr')).to_be_visible()
        await page.locator('#workflowSalePick').select_option('7')
        await expect(form).to_have_attribute('data-pending-keys', first_keys)
        await page.reload()
        await expect(form).to_have_attribute('data-pending-keys', first_keys)
        await page.clock.fast_forward(31000)
        await expect(form.locator('[data-operation-progress]')).to_contain_text(re.compile('Failed to fetch|сохранена сервером'), timeout=10000)
        assert faults['lost'] and len(drafts[7]['photos']) == 3
        if 7 not in sales:
            await page.clock.fast_forward(61000)
        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=15000)
        assert len(sales) == 2 and len(drafts[7]['photos']) == 3
        assert sum(call['filename'] == 'one.png' for call in calls) == 1
        assert sum(call['filename'] == 'two.png' for call in calls) == 1
        assert len({call['key'] for call in calls if call['filename'] == 'three.png'}) == 1
        paid.add(7)
        unverified.add(7)
        await page.clock.fast_forward(6000)
        await expect(form.locator('[data-workflow=gallery-sync]')).to_be_visible()
        await expect(form.locator('[data-sale-client-link]')).to_have_count(0)
        await form.locator('[data-workflow=gallery-sync]').click()
        await expect(form.locator('[data-sale-client-link]')).to_be_visible()
        await page.screenshot(path=str(output/f'{label}.png'), full_page=True)
        # Three automatic retries stop after 30/60/120s, retaining the original
        # key/blob. Foreground and elapsed time cannot silently restart it.
        await page.locator('#workflowSalePick').select_option('9')
        await form.locator('[name=selected]').set_input_files({'name': 'paused.png', 'mimeType': 'image/png', 'buffer': png('purple')})
        await form.locator('[name=receipt]').set_input_files({'name': 'receipt-3.png', 'mimeType': 'image/png', 'buffer': png('cyan')})
        await form.get_by_role('button', name='Сохранить продажу', exact=True).click()
        wait_counter = """async n=>{
          const rows=await(await import('/app/js/outbox.js')).outboxRows();
          const keys=JSON.parse(document.querySelector('#workflowSale').dataset.pendingKeys||'[]');
          const file=rows.find(row=>keys.includes(row.key)&&row.kind==='sale_selected');
          return file?.saleTransientFailures===n&&file.status!=='syncing';
        }"""
        async def wait_count(count):
            for _ in range(200):
                if await page.evaluate(wait_counter, count):
                    return
                await asyncio.sleep(.05)
            raise AssertionError(f'Retry counter did not reach {count}')
        await wait_count(1)
        paused_keys = await form.get_attribute('data-pending-keys')
        for delay, count in [(31000, 2), (61000, 3), (121000, 4)]:
            await page.clock.fast_forward(delay)
            await wait_count(count)
        await expect(form.locator('[data-operation-progress]')).to_contain_text('Передача приостановлена')
        await page.clock.fast_forward(600000)
        assert sum(call['filename'] == 'paused.png' for call in calls) == 4
        assert 9 not in sales
        await form.get_by_role('button', name='Продолжить отправку', exact=True).click()
        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=15000)
        await expect(form).to_have_attribute('data-pending-keys', paused_keys)
        assert len(sales) == 3 and sum(call['filename'] == 'paused.png' for call in calls) == 5
        assert len({call['key'] for call in calls if call['filename'] == 'paused.png'}) == 1
        # Inline new guest is the same one-button chain, with no full-shoot
        # count invented. A definitive rejected start may be corrected in place.
        await form.locator('[data-workflow=another-sale]').click()
        await page.locator('#workflowSalePick').select_option('-1')
        await form.locator('[name=guest_name]').fill('Гость с одинаковым именем')
        await form.locator('[name=photographer_id]').select_option('2')
        await form.locator('[name=selected]').set_input_files([
            {'name': 'walk-in-a.png', 'mimeType': 'image/png', 'buffer': png('brown')},
            {'name': 'walk-in-b.png', 'mimeType': 'image/png', 'buffer': png('pink')}])
        await form.locator('[name=receipt]').set_input_files({'name': 'walk-in-receipt.png', 'mimeType': 'image/png', 'buffer': png('navy')})
        await expect(form.locator('[name=total]')).to_have_value('2')
        await expect(form.locator('[name=sold]')).to_have_value('2')
        await form.screenshot(path=str(output/f'{label}-minimal-new-guest.png'))
        await form.get_by_role('button', name='Сохранить продажу', exact=True).click()
        await expect(form.locator('[data-operation-progress]')).to_contain_text('Новая продажа не создана')
        inline_keys = await form.get_attribute('data-pending-keys')
        assert 10 not in drafts and 10 not in sales
        await form.get_by_role('button', name='Исправить данные нового гостя', exact=True).click()
        await expect(form.locator('[name=guest_name]')).to_have_value('Гость с одинаковым именем')
        await expect(form.locator('[name=photographer_id]')).to_have_value('2')
        await form.get_by_role('button', name='Сохранить исправление и продолжить', exact=True).click()
        await expect(form.locator('[data-operation-progress]')).to_contain_text('сохранена сервером', timeout=15000)
        await expect(form).to_have_attribute('data-pending-keys', inline_keys)
        assert len(sales) == 4 and drafts[10]['sold'] == drafts[10]['total'] == 2
        assert len({call['key'] for call in calls if call['kind'] == 'sale_start_inline'}) == 1
        assert sum(call['filename'] == 'walk-in-a.png' for call in calls) == 1
        assert sum(call['filename'] == 'walk-in-b.png' for call in calls) == 1
        paid.add(10)
        await form.locator('[data-workflow=gallery-status]').click()
        await expect(form.locator('[data-sale-client-link]')).to_be_visible()
        await form.locator('[data-workflow=gallery-qr]').click()
        await expect(form.locator('.sale-gallery-qr')).to_be_visible()
        # A fresh, independent inline form has no predecessor's pending keys.
        # Capture its normal 500-RUB quote after the earlier correction.
        await form.locator('[data-workflow=another-sale]').click()
        await page.locator('#workflowSalePick').select_option('-1')
        await form.locator('[name=guest_name]').fill('Гость с одинаковым именем')
        await form.locator('[name=photographer_id]').select_option('2')
        await form.locator('[name=selected]').set_input_files({'name': 'next-guest.png', 'mimeType': 'image/png', 'buffer': png('gold')})
        await form.locator('[name=receipt]').set_input_files({'name': 'next-receipt.png', 'mimeType': 'image/png', 'buffer': png('coral')})
        await expect(form).not_to_have_attribute('data-pending-keys', inline_keys)
        await expect(form.locator('[name=paymentMethod]')).to_have_attribute('type', 'hidden')
        await expect(form.locator('#salePricePreview')).to_contain_text('500')
        await expect(form.locator('[name=total]')).to_have_attribute('type', 'hidden')
        await form.screenshot(path=str(output/f'{label}-minimal-new-guest.png'))
        assert len(sales) == 4, 'Choosing files alone cannot create the next sale'
        assert not errors, errors
        assert await page.locator('#errors').inner_text() == ''
        return {'sales': len(sales), 'first_photos': len(drafts[7]['photos']),
                'third_file_attempts': sum(call['filename'] == 'three.png' for call in calls),
                'inline_start_attempts': sum(call['kind'] == 'sale_start_inline' for call in calls),
                'paused_file_attempts': sum(call['filename'] == 'paused.png' for call in calls)}
    finally:
        await context.close()
        await server.close()


async def main():
    output = Path(os.getenv('ONE_CLICK_QA_DIR', '/tmp/photo-boss-one-click-qa'))
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
        try:
            results = await asyncio.gather(*[
                scenario(browser, viewport, label, output)
                for label, viewport in [('desktop', {'width': 1440, 'height': 1000}),
                                        ('mobile', {'width': 390, 'height': 844})]])
            for label, result in zip(('desktop', 'mobile'), results, strict=True):
                print(label, json.dumps(result), flush=True)
        finally:
            await browser.close()
    print('PASS one-button save, stable keys, 2/3 accepted, independent sale, reload, failed/lost file recovery, paid-only gallery and manager QR')


if __name__ == '__main__':
    asyncio.run(main())
