"""Deterministic recorder lifecycle checks; live media is covered by work_calls_browser_check."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photo-boss-recorder.invalid"

MOCK_MEDIA = """window.captureMode='normal';window.streams=[];window.recorders=[];window.sentFiles=[];
window.recorderStartFails=false;window.recordingBytes=64;window.pendingCapture=null;
Object.defineProperty(HTMLMediaElement.prototype,'srcObject',{configurable:true,
    get(){return this.mockStream||null;},set(value){this.mockStream=value;}});
function makeStream(config){
    const tracks=[];
    for(const kind of ['audio','video'])if(config[kind])tracks.push({kind,readyState:'live',
        stop(){this.readyState='ended';},getSettings(){return {facingMode:'user'};}});
    const stream={getTracks:()=>tracks,getAudioTracks:()=>tracks.filter(t=>t.kind==='audio'),
        getVideoTracks:()=>tracks.filter(t=>t.kind==='video'),addTrack:t=>tracks.push(t),
        removeTrack:t=>{const i=tracks.indexOf(t);if(i>=0)tracks.splice(i,1);}};
    streams.push(stream);return stream;
}
Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{
    getUserMedia:config=>{
        if(captureMode==='denied')return Promise.reject(new DOMException('Denied','NotAllowedError'));
        if(captureMode==='pending')return new Promise(resolve=>{pendingCapture=()=>resolve(makeStream(config));});
        return Promise.resolve(makeStream(config));
    }
}});
window.MediaRecorder=class {
    static isTypeSupported(){return true;}
    constructor(stream,options){this.stream=stream;this.mimeType=options.mimeType;this.state='inactive';recorders.push(this);}
    start(){this.state='recording';if(recorderStartFails)throw new DOMException('Cannot start','NotReadableError');}
    stop(){this.state='inactive';queueMicrotask(()=>{
        this.ondataavailable?.({data:new Blob([new Uint8Array(recordingBytes)],{type:this.mimeType})});
        this.onstop?.();
    });}
};
const createURL=URL.createObjectURL.bind(URL),revokeURL=URL.revokeObjectURL.bind(URL);
window.previewURLs=new Set();
URL.createObjectURL=blob=>{const url=createURL(blob);previewURLs.add(url);return url;};
URL.revokeObjectURL=url=>{previewURLs.delete(url);revokeURL(url);};
"""


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 640})
        errors = []
        page.on("pageerror", lambda event: errors.append(str(event)))
        await page.add_init_script(MOCK_MEDIA)

        async def route(request):
            path = request.request.url.removeprefix(BASE).split("?", 1)[0]
            if path == "/":
                return await request.fulfill(content_type="text/html", body="""<!doctype html>
                <html lang="ru"><head><meta name="viewport" content="width=device-width,initial-scale=1">
                <link rel="stylesheet" href="/work-chat/chat.css"></head><body>
                <button id="focus">Записать</button><script type="module">
                import {openRecorder,closeRecorder} from '/work-chat/recorder.js';
                window.openFixture=kind=>openRecorder(kind,window.fixtureSend||
                    (async file=>{sentFiles.push({name:file.name,size:file.size,type:file.type});}));
                window.closeFixture=closeRecorder;window.fixtureReady=true;
                </script></body></html>""")
            files = {
                "/work-chat/recorder.js": "app/work_chat_ui/recorder.js",
                "/work-chat/camera.js": "app/work_chat_ui/camera.js",
                "/work-chat/ui.js": "app/work_chat_ui/ui.js",
                "/work-chat/chat.css": "app/work_chat_ui/chat.css",
                "/app/js/domain.js": "app/webapp/js/domain.js",
            }
            if path in files:
                return await request.fulfill(body=(ROOT / files[path]).read_text(),
                                             content_type="text/css" if path.endswith(".css") else "text/javascript")
            raise AssertionError(f"Unexpected request: {request.request.url}")

        await page.route("**/*", route)
        try:
            await page.goto(BASE)
            await page.wait_for_function("window.fixtureReady")
            await page.locator("#focus").focus()

            # Cancel a slow permission prompt: a late stream must be stopped.
            await page.evaluate("()=>{captureMode='pending';window.opening=openFixture('audio');}")
            await page.wait_for_function("() => typeof window.pendingCapture === 'function'")
            await page.locator("[data-record-close]").click()
            await page.evaluate("pendingCapture();opening.then(()=>{captureMode='normal';})")
            await page.wait_for_function("captureMode==='normal'")
            assert await page.evaluate("streams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")
            assert await page.locator("#focus").evaluate("e=>e===document.activeElement")

            # Permission denial and encoder failure leave no active device or send controls.
            await page.evaluate("captureMode='denied';openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=error]").wait_for()
            assert "к микрофону" in await page.locator("[data-record-error]").inner_text()
            await page.locator("[data-record-close]").click()
            await page.evaluate("captureMode='normal';recorderStartFails=true;openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=error]").wait_for()
            assert await page.locator("[data-record-stop]").is_hidden()
            assert await page.evaluate("streams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")
            await page.locator("[data-record-close]").click()
            await page.evaluate("recorderStartFails=false")

            # A recorder error can also emit a stop event: never turn failed capture into a preview.
            await page.evaluate("openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.evaluate("const r=recorders.at(-1),stop=r.onstop;r.onerror();stop();")
            await page.locator(".pb-record-dialog[data-phase=error]").wait_for()
            assert await page.locator("[data-record-send]").is_hidden()
            assert await page.evaluate("streams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')")
            await page.locator("[data-record-close]").click()

            # Late events from a canceled capture cannot release a new capture.
            await page.evaluate("openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.evaluate("window.lateError=recorders.at(-1).onerror;closeFixture();openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.evaluate("lateError()")
            assert await page.evaluate("streams.at(-1).getTracks().every(t=>t.readyState==='live')")
            await page.locator("[data-record-stop]").click()
            await page.locator("[data-record-send]").wait_for()

            # Retry retains the preview. An old rejected send cannot resurrect a reset session.
            await page.evaluate("window.fixtureSend=null")
            await page.locator("[data-record-send]").click()
            await page.wait_for_function("!document.querySelector('.pb-record-dialog').open")
            assert await page.evaluate("sentFiles.length===1&&sentFiles[0].name.endsWith('.weba')&&previewURLs.size===0")
            await page.evaluate("retryAttempts=[];retryFails=true;fixtureSend=async file=>{retryAttempts.push(file.size);if(retryFails){retryFails=false;throw new Error('Fixture upload failed');}sentFiles.push({name:file.name,size:file.size,type:file.type});};openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.locator("[data-record-stop]").click()
            await page.locator("[data-record-send]").click()
            await page.locator(".pb-record-dialog[data-phase=preview]").wait_for()
            assert await page.locator("[data-record-send]").is_enabled()
            assert "Fixture upload failed" in await page.locator("[data-record-error]").inner_text()
            await page.locator("[data-record-send]").click()
            await page.wait_for_function("!document.querySelector('.pb-record-dialog').open")
            assert await page.evaluate("retryAttempts.length===2&&retryAttempts[0]===retryAttempts[1]&&previewURLs.size===0")
            await page.evaluate("fixtureSend=()=>new Promise((resolve,reject)=>{window.rejectOldSend=reject;});openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.locator("[data-record-stop]").click()
            await page.locator("[data-record-send]").click()
            await page.locator(".pb-record-dialog[data-phase=sending]").wait_for()
            await page.evaluate("closeFixture();fixtureSend=null;openFixture('audio')")
            await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
            await page.evaluate("rejectOldSend(new Error('Old account upload failed'))")
            assert await page.locator("[data-record-error]").is_hidden()
            assert await page.locator(".pb-record-dialog").get_attribute("data-phase") == "recording"
            assert await page.evaluate("streams.at(-1).getTracks().every(t=>t.readyState==='live')")
            await page.locator("[data-record-close]").click()
            for byte_size in (0, 21 * 1024 * 1024):
                await page.evaluate("bytes=>{recordingBytes=bytes;openFixture('audio');}", byte_size)
                await page.locator(".pb-record-dialog[data-phase=recording]").wait_for()
                await page.locator("[data-record-stop]").click()
                await page.locator(".pb-record-dialog[data-phase=error]").wait_for()
                assert await page.locator("[data-record-send]").is_hidden()
                assert await page.evaluate("streams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')&&previewURLs.size===0")
                await page.locator("[data-record-close]").click()
            assert await page.evaluate("streams.flatMap(s=>s.getTracks()).every(t=>t.readyState==='ended')&&previewURLs.size===0")
            assert not errors, errors
            print(json.dumps({"ok": True, "latePermissionRelease": True, "permissionAndEncoderFailure": True,
                              "staleRecorderEvents": True, "sendRetryAndReset": True, "devicesReleased": True}))
        finally:
            await page.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
