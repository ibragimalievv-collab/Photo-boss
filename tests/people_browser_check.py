"""Browser fixtures only, no real server/token/location or personal data."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'qa-people'
OUT.mkdir(exist_ok=True)
ROLES = ['OWNER', 'ADMIN', 'PHOTOGRAPHER', 'MANAGER']
BASE = 'https://photo-boss-test.invalid'


def fixture_handler(me, data, sent, theme):
    def route(r):
        path = r.request.url.removeprefix(BASE).split('?', 1)[0]
        if r.request.method in ('POST', 'PUT'):
            sent.append(json.loads(r.request.post_data))
            return r.fulfill(json={'employee': data['items'][0]}, status=201)
        if path == '/api/miniapp/me':
            return r.fulfill(json=me)
        if path == '/api/miniapp/people':
            return r.fulfill(json=data)
        if path == '/api/miniapp/documents':
            return r.fulfill(json={'general': 'Проект. Владелец имеет доступ ко всем рабочим чатам, включая диалоги.',
                'version': 'draft-test', 'servicesContract': 'Не подготовлен', 'dataConsent': 'Отдельный документ.'})
        files = {'/people/people.js': ROOT/'app/people_ui/people.js', '/people/people.css': ROOT/'app/people_ui/people.css',
                 '/app/js/api.js': ROOT/'app/webapp/js/api.js', '/app/js/domain.js': ROOT/'app/webapp/js/domain.js'}
        if path in files:
            return r.fulfill(body=files[path].read_text(), content_type='text/css' if path.endswith('.css') else 'text/javascript')
        if path == '/':
            return r.fulfill(content_type='text/html',body=f'<!doctype html><html lang="ru" data-theme="{theme}"><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><header id="topbar"><b>Photo Boss</b></header><script>window.Telegram={{WebApp:{{initData:"fixture-only"}}}}</script><script type="module" src="/people/people.js"></script></body></html>')
        raise AssertionError('Unexpected request '+r.request.url)
    return route


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for role in ROLES:
            for width in (320, 390, 768):
                for theme in ('premium', 'light', 'photo'):
                    page = browser.new_page(viewport={'width': width, 'height': 844})
                    errors, sent = [], []
                    page.on('pageerror', errors.append)
                    me = {'user': {'id': 1, 'name': 'Test', 'roles': [role]},
                          'permissions': {'manageSchedule': role in ('OWNER', 'ADMIN')}}
                    data = {'items': [{'id': 3, 'name': '<script>bad()</script>', 'telegramId': 1003,
                             'active': True, 'roles': ['PHOTOGRAPHER'], 'hotelIds': [1], 'editable': True, 'revision': 'a'*64}],
                            'hotels': [{'id': 1, 'name': 'Test hotel'}], 'canAssignAdmin': role == 'OWNER', 'next': None}
                    page.route('**/*', fixture_handler(me, data, sent, theme))
                    page.goto(BASE)
                    page.locator('[data-open-people]').click()
                    if role in ('OWNER','ADMIN'):
                        page.locator('[data-people="add"]').click()
                        page.locator('[name="name"]').fill('Test employee')
                        page.locator('[name="telegramId"]').fill('2001')
                        page.locator('[name="role"][value="PHOTOGRAPHER"]').check()
                        assert page.locator('[name="role"][value="ADMIN"]').count() == (1 if role=='OWNER' else 0)
                        page.locator('[name="hotel"]').check()
                        page.locator('[type="submit"]').click()
                        page.locator('#pbPeopleRows').wait_for()
                        assert sent and sent[0]['name']=='Test employee'
                        assert page.locator('#pbPeopleRows script').count()==0
                        page.locator('[data-people="documents"]').click()
                    page.get_by_text('Не подписано. Подписание отключено.', exact=True).wait_for()
                    assert page.locator('.pb-people-rules').evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)') >= 16
                    assert page.locator('.pb-people-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+2')
                    page.screenshot(path=str(OUT/f'{role}-{width}-{theme}.png'))
                    page.locator('[data-people="close"]').click()
                    assert not page.locator('dialog').is_visible()
                    assert not errors, errors
                    results.append({'role':role,'width':width,'theme':theme,'ok':True})
                    page.close()
        browser.close()
    (OUT/'results.json').write_text(json.dumps(results,indent=2))
    print(f'{len(results)} fixture browser scenarios passed; no live Telegram or database used.')


if __name__=='__main__':
    main()
