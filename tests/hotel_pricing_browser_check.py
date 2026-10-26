"""Same-session pricing edits retain the mounted sale form and picked files.

Uses real browser DOM/events with an isolated versioned-tariff HTTP stub; no
production prices, photos, receipts or financial records are changed.
"""
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://hotel-pricing.invalid'


async def main():
    catalogue = {
        'actorId': 1, 'canConfigurePricing': True,
        'hotels': [{'id': 1, 'name': 'Fixture hotel', 'fixedUnitTariffId': 1, 'unitPrice': '400'}],
        'tariffs': [{'id': 1, 'hotelId': 1, 'name': 'Fixed frame', 'kind': 'UNIT', 'price': '400'},
                    {'id': 2, 'hotelId': 1, 'name': 'Package', 'kind': 'PACKAGE', 'price': '1000', 'photoCount': 3}],
        'albums': [{'id': 3, 'hotelId': 1, 'name': 'Album', 'price': '10000'}],
        'bookings': [{'id': 7, 'hotelId': 1, 'price': '400', 'paidAmount': '0', 'salesAmount': '0',
                      'pricing': {'kind': 'UNIT', 'price': '400', 'tariffId': 1}}],
    }
    html = '''<!doctype html><html><body><div id="pricing"></div><div id="sale"></div>
    <script type="module">
    import {pricingView,tariffOptions} from '/hotel-pricing.js';
    window.catalogue=CATALOGUE;
    document.querySelector('#pricing').innerHTML=pricingView(window.catalogue);
    window.mountSale=()=>{document.querySelector('#sale').innerHTML=`<form id="workflowSale" data-draft-scope="7">
      ${tariffOptions(window.catalogue.bookings[0],window.catalogue.tariffs)}
      <input name="total" value="5"><input name="sold" value="2">
      <input type="file" name="receipt"><input type="file" name="selected" multiple>
      </form>`;};window.mountSale();window.ready=true;
    </script></body></html>'''.replace('CATALOGUE', json.dumps(catalogue))
    api = '''export async function api(path,options){
      window.priceRequests??=[];window.priceRequests.push({path,...options});
      window.priceNextId=(window.priceNextId||9)+1;return {id:window.priceNextId};
    }'''
    domain = '''export const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));'''
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None)
        try:
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(request):
                path = request.request.url.removeprefix(BASE)
                if path == '/':
                    return await request.fulfill(body=html, content_type='text/html')
                source = {'/hotel-pricing.js': (ROOT / 'app/webapp/js/hotel-pricing.js').read_text(),
                          '/api.js': api, '/domain.js': domain}.get(path)
                if source is None:
                    return await request.abort()
                await request.fulfill(body=source, content_type='text/javascript')

            await page.route(BASE + '/**', route)
            await page.goto(BASE + '/')
            await page.wait_for_function('window.ready===true')
            await page.locator('#pricing > details > summary').click()
            form = page.locator('#hotelPricingForm')
            await page.locator('#workflowSale [name=receipt]').set_input_files(
                {'name': 'receipt.jpg', 'mimeType': 'image/jpeg', 'buffer': b'receipt bytes'})
            await page.locator('#workflowSale [name=selected]').set_input_files(
                {'name': 'selected.jpg', 'mimeType': 'image/jpeg', 'buffer': b'selected original bytes'})
            await page.evaluate('''()=>{window.originalSaleForm=document.querySelector('#workflowSale');
                window.originalReceipt=window.originalSaleForm.elements.receipt;
                window.originalSelected=window.originalSaleForm.elements.selected;}''')

            # A successful edit returns a fresh ID; the replacement row must be
            # usable immediately for another edit, without a workflow refresh.
            await page.locator('[data-tariff-edit="2"]').click()
            await form.locator('[name=price]').fill('1500')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-tariff-edit="10"]')).to_be_visible()
            await expect(page.locator('[data-tariff-edit="2"]')).to_have_count(0)
            await page.locator('#workflowSale [name=tariffId]').select_option('10')
            await page.locator('[data-tariff-edit="10"]').click()
            await expect(form.locator('[name=replaces]')).to_have_value('10')
            await form.locator('[name=name]').fill('Package edited twice')
            await form.locator('[name=price]').fill('1700')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-tariff-edit="11"]')).to_be_visible()
            await expect(page.locator('[data-tariff-edit="10"]')).to_have_count(0)
            await expect(page.locator('#workflowSale [name=tariffId]')).to_have_value('')
            await expect(page.locator('[data-pricing-selection-notice]')).to_contain_text('проверьте сумму')

            # Newly created packages and albums appear both in the edit list and
            # in the existing sale selectors, without replacing its file inputs.
            await form.locator('[name=name]').fill('New package')
            await form.locator('[name=kind]').select_option('PACKAGE')
            await form.locator('[name=price]').fill('2000')
            await form.locator('[name=photoCount]').fill('5')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-tariff-edit="12"]')).to_be_visible()
            await page.locator('#workflowSale [name=tariffId]').select_option('12')
            await form.locator('[name=name]').fill('New album')
            await form.locator('[name=kind]').select_option('ALBUM')
            await form.locator('[name=price]').fill('12000')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-album-edit="13"]')).to_be_visible()
            await page.locator('#workflowSale [name=albumId]').select_option('13')
            await expect(page.locator('#workflowSale [name=tariffId]')).to_have_value('12')
            await page.locator('[data-album-edit="13"]').click()
            await expect(form.locator('[name=replaces]')).to_have_value('13')
            await form.locator('[name=price]').fill('13000')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-album-edit="14"]')).to_be_visible()
            await expect(page.locator('[data-album-edit="13"]')).to_have_count(0)
            await page.locator('[data-album-edit="14"]').click()
            await expect(form.locator('[name=replaces]')).to_have_value('14')
            await expect(form.locator('[name=price]')).to_have_value('13000')
            assert await page.evaluate('''async()=>{
              const sale=document.querySelector('#workflowSale');
              return sale===window.originalSaleForm&&sale.elements.receipt===window.originalReceipt&&
                sale.elements.selected===window.originalSelected&&
                await sale.elements.receipt.files[0].text()==='receipt bytes'&&
                await sale.elements.selected.files[0].text()==='selected original bytes';
            }''')
            assert await page.evaluate('window.priceRequests.length') == 5
            # A previously agreed draft keeps its own price and album snapshot
            # when the administrator changes the catalogue for new bookings.
            await page.evaluate('''()=>{window.catalogue.bookings[0].draft={pricing:{kind:'UNIT',
              price:'400',tariffId:77,lines:[{type:'ALBUM',albumId:3,name:'Frozen album',price:'10000',quantity:2}]}};
              window.mountSale();}''')
            await page.locator('[data-tariff-edit="1"]').click()
            await form.locator('[name=price]').fill('500')
            await form.get_by_role('button', name='Сохранить тариф', exact=True).click()
            await expect(page.locator('[data-tariff-edit="15"]')).to_be_visible()
            await expect(page.locator('#workflowSale [name=tariffId]')).to_have_value('77')
            await expect(page.locator('#workflowSale [name=tariffId]')).to_be_disabled()
            await expect(page.locator('#workflowSale [name=albumId]')).to_have_value('3')
            await expect(page.locator('#workflowSale [name=albumId]')).to_be_disabled()
            await expect(page.locator('#workflowSale [name=albumId]')).to_contain_text('10000')
            assert await page.evaluate("window.catalogue.bookings[0].draft.pricing.price") == '400'
            assert not errors, errors
            print('PASS: same-session second tariff edit, new package/album visibility, updated sale options, stale choice warning and original sale file inputs retained')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
