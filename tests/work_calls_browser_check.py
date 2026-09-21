"""Three real WebRTC browser clients, fixture staff, HTTP signaling, fake devices.

Run separately with playwright installed. No production API or Telegram messages.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright
from test_miniapp_release import signed
from test_work_calls import CallsTests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photo-boss-calls.invalid"
RELAY_ONLY = os.getenv("CALLS_QA_RELAY_ONLY") == "1"
RINGING = """()=>testRingAnalysers.some(a=>{const v=new Float32Array(a.fftSize);
    a.getFloatTimeDomainData(v);return v.some(x=>Math.abs(x)>0.005);})"""
SILENT = """()=>testRingAnalysers.every(a=>{const v=new Float32Array(a.fftSize);
    a.getFloatTimeDomainData(v);return v.every(x=>Math.abs(x)<0.0001);})"""


async def wait_async(page, predicate, *, arg=None, timeout=30000):
    # Explicitly await async predicates. wait_for_function can treat their Promise
    # as truthy before its eventual False result and produce a false positive.
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await page.evaluate(predicate, arg):
            return
        await page.wait_for_timeout(150)
    raise AssertionError("Timed out waiting for actual browser media: " + predicate[:120])


async def main():
    fixture = CallsTests()
    await fixture.asyncSetUp()
    files = {}

    async def upload(path, payload, **kwargs):
        files[path] = payload

    async def download(path, **kwargs):
        return files[path]

    fixture.client.server.app['yandex_disk'] = SimpleNamespace(state={'connected': True},
        ensure_dir=AsyncMock(), upload_bytes=AsyncMock(side_effect=upload),
        download_bytes=AsyncMock(side_effect=download),
        delete=AsyncMock(side_effect=lambda path: files.pop(path, None)))
    errors = []
    contexts = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.getenv("CALLS_CHROMIUM_PATH") or None,
            args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        try:
            async def route(request):
                path = request.request.url.removeprefix(BASE)
                if path.startswith("/api/miniapp/"):
                    response = await fixture.client.request(request.request.method, path,
                        data=request.request.post_data_buffer,
                        headers={"X-Telegram-Init-Data": request.request.headers.get("x-telegram-init-data", ""),
                                 "Content-Type": request.request.headers.get('content-type', 'application/json')})
                    return await request.fulfill(status=response.status, body=await response.read(), content_type=response.content_type)
                if path == "/":
                    return await request.fulfill(content_type="text/html", body="""<!doctype html>
                        <html lang="ru" data-theme="light"><head><meta name="viewport" content="width=device-width,initial-scale=1">
                        <link rel="stylesheet" href="/app/css/styles.css"></head><body>
                        <header id="topbar" class="topbar"><b>Photo Boss</b></header>
                        <script type="module" src="/work-chat/chat.js"></script></body></html>""")
                if path.startswith("/work-chat/"):
                    file = ROOT / "app/work_chat_ui" / path.rsplit("/", 1)[1]
                else:
                    file = ROOT / "app/webapp" / path.removeprefix("/app/")
                return await request.fulfill(body=file.read_text(),
                    content_type="text/css" if path.endswith(".css") else "text/javascript")

            pages = []
            for uid in (1, 4, 5):
                context = await browser.new_context(viewport={"width":390,"height":844},
                    permissions=["camera", "microphone"])
                contexts.append(context)
                await context.add_init_script("window.Telegram={WebApp:{initData:" + json.dumps(signed(uid+1000)) + "}}")
                # Instrument real browser APIs only to inspect stats and track cleanup.
                await context.add_init_script("window.testRelayOnly=" + json.dumps(RELAY_ONLY) + ";")
                await context.add_init_script("""window.testTrackEvents=[];window.testPlayErrors=[];const nativePlay=HTMLMediaElement.prototype.play;HTMLMediaElement.prototype.play=function(){const result=nativePlay.call(this);result?.catch(e=>testPlayErrors.push(e.name+": "+e.message));return result;};window.testPCs=[];window.testStreams=[];window.testRingAnalysers=[];window.testIceErrors=[];
                    const RealPC=window.RTCPeerConnection;
                    window.RTCPeerConnection=class extends RealPC{constructor(config){
                        super({...config,...(testRelayOnly?{iceTransportPolicy:'relay'}:{})});testPCs.push(this);
                        this.addEventListener('track',e=>testTrackEvents.push({kind:e.track.kind,id:e.track.id,streams:e.streams.map(s=>s.id)}));this.addEventListener('icecandidateerror',e=>testIceErrors.push({code:e.errorCode,text:e.errorText,url:e.url}));}};
                    const RealAudio=window.AudioContext;
                    window.AudioContext=class extends RealAudio{createGain(){
                        const g=super.createGain(),connect=g.connect.bind(g);
                        g.connect=(...args)=>{if(args[0]===this.destination){const a=this.createAnalyser();
                            connect(a);testRingAnalysers.push(a);}return connect(...args);};return g;}};
                    const get=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                    // The runner has one synthetic camera. Map facing requests to that real
                    // capture device and verify constraints, replacement and recovery separately.
                    window.testCameraRequests=[];window.testCameraFailure=false;
                    navigator.mediaDevices.getUserMedia=async (config)=>{
                        const facing=config.video?.facingMode;
                        if(facing)testCameraRequests.push(facing.exact||facing.ideal||facing);
                        if(testCameraFailure&&facing?.exact){testCameraFailure=false;throw new DOMException('fixture camera unavailable','OverconstrainedError');}
                        const adjusted=config.video?{...config,video:{...config.video,facingMode:undefined}}:config;
                        const s=await get(adjusted);testStreams.push(s);return s;};""")
                page = await context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.route("**/*", route)
                await page.goto(BASE)
                await page.locator("[data-open-chat]").click()
                await page.locator('[data-chat="general"]').click()
                pages.append(page)
            a, b, c = pages
            if not RELAY_ONLY:
                for mode in ('audio', 'video'):
                    await a.locator(f'[data-record="{mode}"]').click()
                    if mode == 'video':
                        await a.locator('[data-record-camera]').click()
                        await a.wait_for_function("!document.querySelector('[data-record-camera]').disabled")
                        assert await a.evaluate("testCameraRequests.at(-1)==='environment'")
                        assert await a.locator('[data-record-live]').evaluate("e=>getComputedStyle(e).transform==='none'")
                        await a.locator('[data-record-start]').click()
                    await a.locator('[data-record-stop]:not([disabled])').wait_for()
                    await a.wait_for_timeout(1500)  # Record real synthetic media frames.
                    await a.locator('[data-record-stop]').click()
                    await a.locator('[data-record-send]').wait_for()
                    assert await a.evaluate("testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")
                    await a.locator('[data-record-send]').click()
                    await a.wait_for_function("!document.querySelector('.pb-record-dialog').open")
                    await b.locator('[data-load-media]').last.wait_for(timeout=10000)
                    await b.locator('[data-load-media]').last.click()
                    await b.wait_for_function("""tag=>{const m=[...document.querySelectorAll('.pb-chat-media '+tag)].at(-1);
                        return m&&!m.hidden&&m.currentTime>0;}""", arg=mode, timeout=8000)
                    await b.evaluate("document.querySelectorAll('.pb-chat-media audio,.pb-chat-media video').forEach(m=>m.pause())")
                assert len(files) == 2
                deleted_id = await a.locator('.pb-chat-message.mine').first.get_attribute('data-message-id')
                assert await b.locator('[data-delete-message]').count() == 0
                await a.locator('.pb-chat-message.mine summary').first.click()
                await a.locator(f'[data-delete-message="{deleted_id}"]').click()
                await a.locator('[data-delete-confirm]').click()
                await a.wait_for_function("!document.querySelector('.pb-chat-delete-dialog').open")
                await b.locator(f'[data-message-id="{deleted_id}"]').wait_for(state='detached', timeout=12000)
                assert len(files) == 1
                # Canceling a fresh recording must release devices without sending a message.
                await a.locator('[data-record="audio"]').click()
                await a.locator('[data-record-stop]:not([disabled])').wait_for()
                await a.locator('[data-record-close]').click()
                assert len(files) == 1
                assert await a.evaluate("testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")
            await a.locator('[data-start-call="video"]').click()
            for page in (b, c):
                await page.locator('[data-incoming]').wait_for(timeout=15000)
                await page.wait_for_function(RINGING, timeout=8000)
                await page.locator('[data-incoming]').click()
                await page.wait_for_function(SILENT, timeout=2000)
                await page.locator('[data-answer="video"]').click()
            for page in pages:
                await page.wait_for_function("testPCs.filter(p=>p.connectionState==='connected').length===2", timeout=30000)
                await wait_async(page, """async()=>{
                    for(const pc of testPCs.filter(p=>p.connectionState==='connected')) {
                        const reports=[...(await pc.getStats()).values()].filter(x=>x.type==='inbound-rtp');
                        if(!reports.some(x=>x.kind==='audio' && x.bytesReceived>0))return false;
                        if(!reports.some(x=>x.kind==='video' && x.framesDecoded>0))return false;
                    }return true;}""", timeout=30000)
                print(json.dumps(await page.evaluate("""({playErrors:testPlayErrors,trackEvents:testTrackEvents,
                    callStatus:document.querySelector('[data-call-status]')?.textContent,
                    peers:testPCs.map(p=>({ontrack:String(p.ontrack),receivers:p.getReceivers().map(r=>({id:r.track.id,kind:r.track.kind,muted:r.track.muted})),transceivers:p.getTransceivers().map(t=>({mid:t.mid,direction:t.direction,currentDirection:t.currentDirection,receiver:t.receiver.track.id}))})),
                    videos:[...document.querySelectorAll('.pb-call-tile video')].map(v=>({
                        participant:v.parentElement.dataset.participant,classes:v.parentElement.className,
                        width:v.videoWidth,height:v.videoHeight,ready:v.readyState,paused:v.paused,
                        opacity:getComputedStyle(v).opacity,currentTime:v.currentTime,
                        tracks:v.srcObject?.getTracks().map(t=>({kind:t.kind,ready:t.readyState,muted:t.muted,enabled:t.enabled}))}))})""")))
                print(json.dumps({"pageErrorsBeforeRender": errors}))
                # RTP decoding must also produce visible, playing participant videos.
                await page.wait_for_function("""()=>{
                    const videos=[...document.querySelectorAll('.pb-call-tile.has-video video')];
                    return videos.length===3&&videos.every(v=>v.videoWidth>0&&v.readyState>=2&&!v.paused&&getComputedStyle(v).opacity==='1');
                }""", timeout=10000)
                await page.wait_for_function("""()=>{
                    const players=[...document.querySelectorAll('.pb-call-tile audio')];
                    return players.length===2&&players.every(v=>!v.muted&&!v.paused&&v.currentTime>0&&v.readyState>=2);
                }""", timeout=10000)
                assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
                if RELAY_ONLY:
                    assert await page.evaluate("""async()=>{
                        for(const pc of testPCs.filter(p=>p.connectionState==='connected')){
                            const stats=await pc.getStats();
                            const pairs=[...stats.values()].filter(x=>x.type==='candidate-pair'&&x.nominated&&x.state==='succeeded');
                            if(!pairs.length||pairs.some(x=>stats.get(x.localCandidateId)?.candidateType!=='relay'))return false;
                        }return true;}""")
            if not RELAY_ONLY:
                audio_id = await a.evaluate("testStreams.flatMap(s=>s.getAudioTracks()).find(t=>t.readyState==='live').id")
                for facing in ('environment', 'user'):
                    await a.locator('[data-switch-camera]').click()
                    await a.wait_for_function("!document.querySelector('[data-switch-camera]').disabled")
                    assert await a.evaluate("testCameraRequests.at(-1)") == facing
                    assert await a.evaluate("id=>testStreams.flatMap(s=>s.getAudioTracks()).some(t=>t.id===id&&t.readyState==='live'&&t.enabled)", audio_id)
                    assert await a.locator('.pb-call-tile video').first.evaluate("e=>getComputedStyle(e).transform==='none'")
                before_frames = await b.evaluate("async()=>{let n=0;for(const p of testPCs)for(const x of (await p.getStats()).values())if(x.type==='inbound-rtp'&&x.kind==='video')n+=x.framesDecoded||0;return n;}")
                await wait_async(b, "async n=>{let frames=0;for(const p of testPCs)for(const x of (await p.getStats()).values())if(x.type==='inbound-rtp'&&x.kind==='video')frames+=x.framesDecoded||0;return frames>n+5;}", arg=before_frames)
                await a.evaluate('testCameraFailure=true')
                await a.locator('[data-switch-camera]').click()
                await a.locator('[data-camera-error]:not([hidden])').wait_for()
                assert await a.locator('[data-camera]').get_attribute('aria-pressed') == 'true'
                assert await a.evaluate("testCameraRequests.at(-1)==='user'")
            await a.locator('[data-mic]').click()
            assert await a.evaluate("testStreams.flatMap(s=>s.getAudioTracks()).filter(t=>t.readyState==='live').every(t=>!t.enabled)")
            await a.locator('[data-camera]').click()
            await a.wait_for_function("testStreams.flatMap(s=>s.getVideoTracks()).every(t=>t.readyState==='ended')")
            await a.locator('[data-camera]').click()
            await a.wait_for_function("testStreams.flatMap(s=>s.getVideoTracks()).some(t=>t.readyState==='live')")
            await a.locator('[data-minimize-call]').click()
            await a.locator('[data-resume-call]').click()
            await a.wait_for_function("""()=>[...document.querySelectorAll('.pb-call-tile.has-video video')]
                .every(v=>v.videoWidth>0&&v.readyState>=2&&!v.paused)""", timeout=10000)
            output = Path(os.getenv("CALLS_QA_DIR", "/tmp/photo-boss-calls-qa"))
            output.mkdir(parents=True, exist_ok=True)
            await a.screenshot(path=str(output / "group-call.png"))
            if not RELAY_ONLY:
                print("QA_PREVIEW call.jpg " + base64.b64encode(await a.screenshot(type="jpeg", quality=60)).decode())
            # Ending a group call releases all three clients' microphones/cameras.
            await a.locator('[data-end-all]').click()
            for page in pages:
                await page.wait_for_function("testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')", timeout=15000)
                await page.wait_for_function("testPCs.every(p=>p.connectionState==='closed')", timeout=15000)
                if await page.locator('[data-call-close]').is_visible():
                    await page.locator('[data-call-close]').click()
            # Audio-only private call, accept without camera, then hang up.
            await a.locator('[data-chat="home"]').click()
            await a.locator('[data-peer="4"]').click()
            await a.locator('[data-start-call="audio"]').click()
            await b.locator('[data-incoming]').wait_for(timeout=15000)
            await b.locator('[data-incoming]').click()
            await b.locator('[data-answer="audio"]').click()
            for page in (a,b):
                await page.wait_for_function("testPCs.some(p=>p.connectionState==='connected')", timeout=30000)
                assert await page.evaluate("testStreams.flatMap(s=>s.getVideoTracks()).every(t=>t.readyState==='ended')")
                await wait_async(page, """async()=>{
                    const pcs=testPCs.filter(p=>p.connectionState==='connected');
                    if(pcs.length!==1)return false;
                    const stats=[...(await pcs[0].getStats()).values()];
                    return stats.some(x=>x.type==='inbound-rtp'&&x.kind==='audio'&&x.bytesReceived>0);
                }""")
                await page.wait_for_function("""()=>[...document.querySelectorAll('.pb-call-tile audio')]
                    .some(v=>!v.muted&&!v.paused&&v.currentTime>0&&v.readyState>=2)""", timeout=10000)
            # Enable video in an audio call, then turn it off without losing sound.
            await a.locator('[data-camera]').click()
            await b.wait_for_function("""()=>[...document.querySelectorAll('.pb-call-tile.has-video video')]
                .some(v=>v.videoWidth>0&&!v.paused&&v.readyState>=2)""", timeout=15000)
            await a.locator('[data-camera]').click()
            audio_time = await b.locator('.pb-call-tile audio').evaluate('e=>e.currentTime')
            await b.wait_for_function("""time=>{
                const audio=document.querySelector('.pb-call-tile audio');
                return !audio.muted&&!audio.paused&&audio.currentTime>time+0.3;
            }""", arg=audio_time, timeout=10000)
            await b.locator('[data-hangup]').click()
            for page in (a,b):
                await page.wait_for_function("testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')", timeout=15000)
                if await page.locator('[data-call-close]').is_visible():
                    await page.locator('[data-call-close]').click()
            # Decline and caller cancellation both silence the incoming melody.
            for decline in (True, False):
                await a.locator('[data-start-call="audio"]').click()
                await b.locator('[data-incoming]').wait_for(timeout=15000)
                await b.wait_for_function(RINGING, timeout=8000)
                if decline:
                    await b.locator('[data-ignore]').click()
                else:
                    await a.locator('[data-hangup]').click()
                await b.wait_for_function(SILENT, timeout=8000)
                await a.wait_for_function("testStreams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')", timeout=15000)
                if await a.locator('[data-call-close]').is_visible():
                    await a.locator('[data-call-close]').click()
            assert not errors, errors
            print(json.dumps({"ok":True,"groupParticipants":3,"inboundAudio":True,
                "inboundVideoFrames":True,"privateAudio":True,"muteCameraToggle":True,
                "devicesReleased":True,"ringtone":True,"ringtoneStopsOnAnswerDeclineCancel":True,
                "ownerDeletionPropagates":not RELAY_ONLY,"cameraSwitchAndRecovery":not RELAY_ONLY,"unmirroredVideo":True,"relayOnly":RELAY_ONLY,"voiceAndVideoMessages":not RELAY_ONLY,
                "pageErrors":errors,"screenshot":str(output / "group-call.png")}))
        finally:
            if RELAY_ONLY:
                for context in contexts:
                    for page in context.pages:
                        print(json.dumps(await page.evaluate("""({iceErrors:testIceErrors,
                            states:testPCs.map(p=>({connection:p.connectionState,ice:p.iceConnectionState,gathering:p.iceGatheringState}))})""")))
            for context in contexts:
                await context.close()
            await browser.close()
            await fixture.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(main())
