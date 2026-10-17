"""Record actual Chromium fake-microphone bytes; no production traffic or real audio."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photo-boss-voice-training.invalid"
FIXTURE = """<!doctype html><html lang="ru"><body><main id="fixture"></main>
<script type="module">
import {voiceControls} from '/app/js/voice-training.js';
window.attempts=[];window.streams=[];window.failSend=true;window.lastAudio=null;
const capture=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia=async options=>{const stream=await capture(options);streams.push(stream);return stream;};
window.fixtureApi=async(path,options)=>{
 if(path.endsWith('/voice')){const audio=options.body.get('audio');lastAudio=audio;
  const digest=await crypto.subtle.digest('SHA-256',await audio.arrayBuffer());
  attempts.push({size:audio.size,type:audio.type,digest:Array.from(new Uint8Array(digest)).join(',')});
  if(failSend){failSend=false;throw Error('Fixture network unavailable');}return {ok:true};}
 if(path.endsWith('/speech'))return lastAudio;
 throw Error('Unexpected fixture API path '+path);
};
window.renderFixture=()=>{document.querySelector('#fixture').innerHTML=voiceControls({id:1,revision:1,status:'ACTIVE'});};
renderFixture();window.fixtureReady=true;
</script></body></html>"""


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ])
        context = await browser.new_context(permissions=["microphone"])
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def route(request):
            path = request.request.url.removeprefix(BASE).split("?", 1)[0]
            if path == "/":
                return await request.fulfill(content_type="text/html", body=FIXTURE)
            if path == "/app/js/voice-training.js":
                return await request.fulfill(
                    content_type="text/javascript",
                    body=(ROOT / "app/webapp/js/voice-training.js").read_text())
            if path == "/app/js/api.js":
                return await request.fulfill(content_type="text/javascript",
                    body="export const api=(...args)=>window.fixtureApi(...args);")
            if path == "/app/js/realtime-training.js":
                return await request.fulfill(content_type="text/javascript",
                    body="export const realtimeControls=()=>'';")
            raise AssertionError(f"Unexpected request: {request.request.url}")

        await page.route("**/*", route)
        try:
            await page.goto(BASE)
            await page.wait_for_function("window.fixtureReady")
            await page.locator('[data-voice-action="record"]').click()
            await page.wait_for_function("document.querySelector('[data-voice-status]').textContent.includes('Идёт запись')")
            # A background Academy refresh replaces the controls while capture runs.
            await page.evaluate("renderFixture()")
            await page.locator('[data-voice-action="stop"]').wait_for(state="visible")
            await page.wait_for_function("document.querySelector('[data-voice-level]').value>0")
            await page.locator('[data-voice-action="stop"]').click()
            await page.locator('[data-voice-action="send"]').wait_for(state="visible")
            assert await page.evaluate("streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
            await page.evaluate("renderFixture()")
            await page.locator('[data-voice-action="send"]').wait_for(state="visible")
            assert await page.locator('[data-voice-preview]').get_attribute("src")
            await page.locator('[data-voice-action="send"]').click()
            await page.wait_for_function("attempts.length===1&&document.querySelector('[data-voice-status]').textContent.includes('повторной отправки')")
            await page.locator('[data-voice-action="send"]').click()
            await page.wait_for_function("attempts.length===2&&!document.querySelector('[data-voice-action=record]').disabled")
            assert await page.evaluate("attempts[0].size>100&&attempts[0].digest===attempts[1].digest")
            # Actual application navigation uses replaceState, so hashchange never fires.
            await page.locator('[data-voice-action="record"]').click()
            await page.locator('[data-voice-action="stop"]').wait_for(state="visible")
            await page.wait_for_function("document.querySelector('[data-voice-level]').value>0")
            await page.evaluate("history.replaceState(null,'','#home');document.querySelector('#fixture').innerHTML='Другая страница';")
            await page.wait_for_function("streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
            await page.evaluate("renderFixture()")
            await page.locator('[data-voice-action="send"]').wait_for(state="visible")
            await page.locator('[data-voice-action="cancel"]').click()
            await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
            assert not errors, errors
            print(json.dumps({"ok": True, "realMediaRecorderBytes": await page.evaluate("attempts[0].size"),
                "sameSessionRerender": True, "retryKeepsExactAudio": True,
                "replaceStateNavigationReleasesMicrophone": True}))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
