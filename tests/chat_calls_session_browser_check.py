"""Account-switch races in the real calls UI; isolated HTTPS replies and fake devices.

No production requests, Telegram messages or real microphones/cameras are used.
Run separately with Playwright installed, alongside work_calls_browser_check.py.
"""
import asyncio
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://calls-session.example'

HARNESS = """<!doctype html><html lang="ru"><head>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><header id="topbar">Photo Boss</header><main id="toolbar"></main>
<script type="module">
window.calls=await import('/work-chat/calls.js');
window.setTestUser=id=>{
  Telegram.WebApp.initData=new URLSearchParams({auth_date:String(Math.floor(Date.now()/1000)),
      user:JSON.stringify({id})}).toString();
  calls.initCalls({user:{id,name:`Fixture user ${id}`}});
  document.querySelector('#toolbar').innerHTML=calls.callToolbar(10,'Fixture peer');
};
window.startTestCall=mode=>{
  const target=document.querySelector(`[data-start-call="${mode}"]`);
  window.pendingCall=calls.handleCallClick({target},10,'Fixture peer');
};
document.querySelector('#toolbar').addEventListener('click',e=>{
  if(e.target.closest('[data-start-call]'))window.pendingCall=calls.handleCallClick(e,10,'Fixture peer');
});
window.harnessReady=true;
</script></body></html>"""

INSTRUMENT = """window.Telegram={WebApp:{initData:'',
 enableClosingConfirmation(){window.closingConfirmation=true},
 disableClosingConfirmation(){window.closingConfirmation=false}}};
window.testStreams=[];window.testPCs=[];window.testReplies=[];window.testMediaRequests=[];
window.testDeferMedia=false;window.testMediaPending=false;
const nativeGet=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia=async config=>{
  testMediaRequests.push(config);
  const stream=await nativeGet(config);testStreams.push(stream);
  if(testDeferMedia){testMediaPending=true;await new Promise(resolve=>window.releaseTestMedia=resolve);}
  return stream;
};
const NativePC=window.RTCPeerConnection;
window.RTCPeerConnection=class extends NativePC{
  constructor(config){super(config);testPCs.push(this);}
};
const nativeFetch=window.fetch.bind(window);
window.fetch=async(...args)=>{
  const response=await nativeFetch(...args),nativeJson=response.json.bind(response);
  response.json=async()=>{const data=await nativeJson();
    testReplies.push({path:new URL(args[0]).pathname,data});return data;};
  return response;
};"""


def room(uid, *, call_id=None, creator=None, group=False):
    creator = uid if creator is None else creator
    return {'id': call_id or f'call-{uid}', 'creatorId': creator,
            'creatorName': f'Fixture user {creator}', 'peerId': 10, 'group': group,
            'mode': 'video', 'participants': [
                {'id': creator, 'name': f'Fixture user {creator}', 'session': f'session-{creator}',
                 'audio': True, 'video': True},
                *([] if group else [{'id': 10, 'name': 'Fixture peer', 'session': 'session-10',
                                    'audio': True, 'video': True}])],
            'status': 'ringing'}


class HeldResponse:
    def __init__(self, data=None):
        self.data = data
        self.seen = asyncio.Event()
        self.release = asyncio.Event()


class Fixture:
    def __init__(self):
        self.requests = []
        self.held = {}
        self.all_holds = []
        self.list_data = {'calls': []}
        self.history_name = 'Current account history'

    def hold(self, endpoint, data=None):
        value = HeldResponse(data)
        self.held[endpoint] = value
        self.all_holds.append(value)
        return value

    async def route(self, route):
        url = urlsplit(route.request.url)
        if url.netloc != 'calls-session.example':
            raise AssertionError('Unexpected external request: ' + route.request.url)
        if url.path == '/':
            return await route.fulfill(body=HARNESS, content_type='text/html')
        if url.path.startswith('/api/miniapp/chat/calls'):
            endpoint = url.path.removeprefix('/api/miniapp/chat/calls')
            auth = parse_qs(route.request.headers.get('x-telegram-init-data', ''))
            uid = json.loads(auth.get('user', ['{}'])[0]).get('id')
            body = json.loads(route.request.post_data or '{}')
            self.requests.append({'path': endpoint, 'user': uid, 'body': body})
            data = {'ok': True}
            if endpoint == '':
                data = self.list_data
            elif endpoint in {'/start', '/join'}:
                data = {'call': room(uid), 'session': f'session-{uid}', 'iceServers': [],
                        'relayConfigured': False}
            elif endpoint == '/sync':
                data = {'call': room(uid), 'signals': [], 'cursor': 0}
            elif endpoint == '/history':
                data = {'items': [{'creatorName': self.history_name, 'mode': 'audio',
                                   'status': 'completed', 'durationSeconds': 21,
                                   'at': '2026-10-08T12:00:00'}]}
            held = self.held.pop(endpoint, None)
            if held:
                data = held.data if held.data is not None else data
                held.seen.set()
                await held.release.wait()
            return await route.fulfill(body=json.dumps(data), content_type='application/json')
        if url.path.startswith('/work-chat/'):
            file = ROOT / 'app/work_chat_ui' / url.path.removeprefix('/work-chat/')
        elif url.path.startswith('/app/'):
            file = ROOT / 'app/webapp' / url.path.removeprefix('/app/')
        else:
            raise AssertionError('Unexpected fixture request: ' + url.path)
        return await route.fulfill(body=file.read_bytes(),
                                  content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')


async def settle(page):
    # Flush real UI continuations after a controlled reply, without depending on
    # an arbitrary delay or treating an async predicate's Promise as truthy.
    await page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


async def check_clean_ui(page):
    assert await page.evaluate("""()=>{
        const panel=document.querySelector('.pb-call-dialog'),banner=document.querySelector('.pb-call-banner');
        return !panel.open&&!panel.textContent.trim()&&banner.hidden&&!banner.textContent.trim()
          &&!document.querySelector('[data-top-call]')&&!window.closingConfirmation;
    }""")


async def check_tracks_ended(page):
    assert await page.evaluate("testStreams.length>0&&testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")


async def main():
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.getenv('CALLS_CHROMIUM_PATH') or None,
            args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'])

        async def scenario():
            context = await browser.new_context(viewport={'width': 390, 'height': 844},
                                                permissions=['camera', 'microphone'])
            await context.add_init_script(INSTRUMENT)
            fixture = Fixture()
            await context.route('**/*', fixture.route)
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(BASE)
            await page.wait_for_function('window.harnessReady')
            return context, page, fixture

        contexts = []
        fixtures = []
        try:
            # Permission/device acquisition can finish after logout. Its newly
            # delivered stream must be stopped, without starting a server call.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            await page.evaluate('setTestUser(1);testDeferMedia=true;startTestCall("video")')
            await page.wait_for_function('testMediaPending')
            await page.evaluate('calls.resetCalls();setTestUser(2);releaseTestMedia()')
            await page.evaluate('()=>window.pendingCall')
            await check_tracks_ended(page)
            await check_clean_ui(page)
            assert not any(r['path'] in {'/start', '/join', '/leave'} for r in fixture.requests)

            # Reset also releases an already acquired stream while /start is
            # still pending. A late success cannot attach an old call or send a
            # leave request with the replacement account's credentials.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            pending_start = fixture.hold('/start')
            await page.evaluate('setTestUser(1);startTestCall("video")')
            await asyncio.wait_for(pending_start.seen.wait(), timeout=10)
            await page.evaluate('calls.resetCalls();setTestUser(2)')
            await check_tracks_ended(page)
            pending_start.release.set()
            await page.evaluate('()=>window.pendingCall')
            await check_tracks_ended(page)
            await check_clean_ui(page)
            assert [(r['path'], r['user']) for r in fixture.requests if r['path'] == '/start'] == [('/start', 1)]
            assert next(r for r in fixture.requests if r['path'] == '/start')['body']['expectedUserId'] == 1
            assert not any(r['path'] in {'/leave', '/sync', '/signal'} for r in fixture.requests)

            # Pending history cannot reopen the prior account's dialog; the
            # replacement account's own history remains usable afterwards.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            fixture.history_name = 'Private old account history'
            history = fixture.hold('/history')
            await page.evaluate('setTestUser(1)')
            await page.locator('[data-call-history]').click()
            await asyncio.wait_for(history.seen.wait(), timeout=10)
            await page.evaluate('calls.resetCalls();setTestUser(2)')
            history.release.set()
            await page.wait_for_function("testReplies.some(r=>r.path.endsWith('/history'))")
            await settle(page)
            await check_clean_ui(page)
            fixture.history_name = 'Replacement account history'
            await page.locator('[data-call-history]').click()
            await expect(page.locator('.pb-call-dialog')).to_contain_text('Replacement account history')
            await expect(page.locator('.pb-call-dialog')).not_to_contain_text('Private old account history')
            assert [r['user'] for r in fixture.requests if r['path'] == '/history'] == [1, 2]
            await page.evaluate('calls.resetCalls()')

            # A stale list must not replace incoming calls belonging to the
            # new identity or reintroduce an old room into the toolbar.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            old_list = fixture.hold('', {'calls': [room(1, call_id='old-room', creator=9, group=True)]})
            await page.evaluate('setTestUser(1)')
            await asyncio.wait_for(old_list.seen.wait(), timeout=10)
            fixture.list_data = {'calls': [room(2, call_id='new-room', creator=8, group=True)]}
            await page.evaluate('calls.resetCalls();setTestUser(2)')
            await expect(page.locator('[data-incoming="new-room"]')).to_be_visible()
            old_list.release.set()
            await page.wait_for_function("testReplies.some(r=>r.data.calls?.some(c=>c.id==='old-room'))")
            await settle(page)
            await expect(page.locator('[data-incoming="new-room"]')).to_be_visible()
            assert await page.locator('[data-incoming="old-room"]').count() == 0
            toolbar = await page.evaluate("calls.callToolbar(null,'Group')")
            assert 'data-join-call="new-room"' in toolbar and 'old-room' not in toolbar
            await page.evaluate('calls.resetCalls()')
            await check_clean_ui(page)

            # A connected UI must immediately release devices/peers when a
            # different user is initialized, even without an explicit reset.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            await page.evaluate('setTestUser(1);startTestCall("video")')
            await page.evaluate('()=>window.pendingCall')
            await expect(page.locator('[data-hangup]')).to_be_visible()
            await page.wait_for_function('testPCs.length===1')
            await page.evaluate('setTestUser(2)')
            await check_tracks_ended(page)
            assert await page.evaluate("testPCs.every(pc=>pc.connectionState==='closed')")
            await check_clean_ui(page)
            assert not any(r['path'] == '/leave' for r in fixture.requests)

            # A failed physical-camera constraint can normally fall back to a
            # fresh capture. If its await crosses an account switch, that
            # fallback must not reopen the camera after reset stopped it.
            context, page, fixture = await scenario()
            contexts.append(context); fixtures.append(fixture)
            await page.evaluate('setTestUser(1);startTestCall("video")')
            await page.evaluate('()=>window.pendingCall')
            await expect(page.locator('[data-switch-camera]')).to_be_visible()
            await page.evaluate("""()=>{
                const track=testStreams[0].getVideoTracks()[0];
                track.applyConstraints=()=>new Promise((resolve,reject)=>{
                    window.testConstraintsPending=true;
                    window.rejectTestConstraints=()=>reject(new DOMException('Fixture switch failure','OverconstrainedError'));
                });
            }""")
            await page.locator('[data-switch-camera]').click()
            await page.wait_for_function('window.testConstraintsPending')
            streams = await page.evaluate('testStreams.length')
            media_requests = await page.evaluate('testMediaRequests.length')
            await page.evaluate('setTestUser(2);rejectTestConstraints()')
            await settle(page)
            await check_tracks_ended(page)
            await check_clean_ui(page)
            assert await page.evaluate('testStreams.length') == streams
            # Count attempts before native acquisition resolves too: an
            # accidentally reopened camera must not slip past a slow device.
            assert await page.evaluate('testMediaRequests.length') == media_requests
            assert not errors, errors
            print(json.dumps({'ok': True, 'lateMediaStopped': True, 'lateStartIgnored': True,
                              'historyIsolated': True, 'callListsIsolated': True,
                              'activeMediaReleased': True, 'noCrossAccountLeave': True,
                              'noCameraFallbackAfterReset': True}))
        finally:
            for fixture in fixtures:
                for held in fixture.all_holds:
                    held.release.set()
            for context in contexts:
                await context.close()
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
