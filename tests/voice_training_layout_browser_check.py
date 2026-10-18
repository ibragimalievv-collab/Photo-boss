"""Measure real voice controls with production CSS; no production traffic or AI.

The installed-app case simulates its touch viewport and standalone JS shell. It
does not claim to install a PWA or verify a physical device's microphone.
"""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright
from realtime_training_browser_check import BASE, FIXTURE, ROOT

FIXTURE = FIXTURE.replace('<html lang="ru">', '<html lang="ru" data-theme="light">').replace(
    '<head>', '<head><meta charset="utf-8">',
).replace(
    "import {realtimeControls} from '/app/js/realtime-training.js';",
    "import {voiceControls} from '/app/js/voice-training.js';",
).replace("innerHTML=realtimeControls(", "innerHTML=voiceControls(")
FIXTURE = FIXTURE.replace(
    '<body><main id="fixture"></main>',
    '<body class="academy-mode"><div class="workspace"><main class="content">'
    '<div class="academy-module"><section class="academy-panel section">'
    '<section id="bookingTraining" class="panel section">'
    '<h3>Голосовая тренировка с гостем</h3><div id="fixture"></div>'
    '</section></section></div></main></div>',
)

CHECK_LAYOUT = """() => {
 const failures=[];
 const visible=node=>{const r=node.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(node).visibility!=='hidden';};
 if(document.documentElement.scrollWidth>innerWidth+1)failures.push('page horizontal overflow');
 for(const root of document.querySelectorAll('.voice-training,.realtime-training')){
  const bounds=root.getBoundingClientRect();
  if(bounds.left<-.5||bounds.right>innerWidth+.5)failures.push('card outside viewport');
  for(const node of root.querySelectorAll('button,audio,meter,select,p,h3,summary,.training-state')){
   if(!visible(node))continue;
   const r=node.getBoundingClientRect();
   const label=node.dataset.voiceAction||node.dataset.realtimeAction||node.tagName;
   if(r.left<bounds.left-.5||r.right>bounds.right+.5)failures.push(label+' outside card');
   if(!['AUDIO','SELECT'].includes(node.tagName)&&node.scrollWidth>node.clientWidth+2)failures.push(label+' clipped text');
   if(node.tagName==='BUTTON'&&r.height<44)failures.push(label+' touch target below 44px');
   // Check each rendered text line, not only the element's box.
   if(node.tagName==='SELECT'||node.tagName==='AUDIO')continue;
   const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
   let text;while(text=walker.nextNode()){
    if(!text.textContent.trim())continue;
    const range=document.createRange();range.selectNodeContents(text);
    for(const line of range.getClientRects())if(line.width>0&&(line.left<r.left-1||line.right>r.right+1||line.top<r.top-1||line.bottom>r.bottom+1))failures.push(label+' text outside element');
   }
  }
  const buttons=[...root.querySelectorAll('button')].filter(visible);
  for(let i=0;i<buttons.length;i++)for(let j=i+1;j<buttons.length;j++){
   const a=buttons[i].getBoundingClientRect(),b=buttons[j].getBoundingClientRect();
   if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)failures.push('overlapping buttons');
  }
 }
 return [...new Set(failures)];
}"""

LARGE_TEXT = """
.voice-training,.realtime-training{font-size:21px}
.voice-training p,.realtime-training p{font-size:21px}
.voice-training h3,.realtime-training h3{font-size:24px}
.voice-training .btn,.realtime-training .btn{font-size:19.5px}
.voice-training .field,.realtime-training .field{font-size:16.5px}
.voice-training select,.realtime-training select{font-size:24px}
.voice-training .small,.realtime-training .small{font-size:18px}
"""


async def page_fixture(context, role):
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def route(request):
        path = request.request.url.removeprefix(BASE).split("?", 1)[0]
        if path == "/":
            return await request.fulfill(content_type="text/html", body=FIXTURE)
        if path == "/app/css/styles.css":
            return await request.fulfill(content_type="text/css",
                                         body=(ROOT / "app/webapp/css/styles.css").read_text())
        if path in {"/app/js/voice-training.js", "/app/js/realtime-training.js", "/app/js/domain.js"}:
            return await request.fulfill(content_type="text/javascript",
                                         body=(ROOT / "app/webapp/js" / path.rsplit("/", 1)[1]).read_text())
        if path == "/app/js/api.js":
            return await request.fulfill(content_type="text/javascript",
                                         body="export const api=(...args)=>window.fixtureApi(...args);")
        raise AssertionError(f"Unexpected request: {request.request.url}")

    await page.route("**/*", route)
    await page.goto(BASE)
    await page.wait_for_function("window.fixtureReady")
    if role == "photographer":
        await page.evaluate("""() => {
          document.body.classList.remove('academy-mode');
          document.querySelector('.academy-module').className='';
          document.querySelector('.academy-panel').className='';
          document.querySelector('#bookingTraining').id='salesTraining';
        }""")
    return page, errors


async def main():
    screenshots = os.getenv("VOICE_LAYOUT_SCREENSHOTS")
    screenshot_dir = Path(screenshots) if screenshots else None
    if screenshot_dir:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ])
        try:
            for width in (320, 390, 768, 1440):
                for role in ("manager", "photographer"):
                    for standalone in (False, True):
                        for large in (False, True):
                            context = await browser.new_context(
                                viewport={"width": width, "height": 1000},
                                permissions=["microphone"],
                                is_mobile=standalone, has_touch=standalone,
                            )
                            if standalone:
                                await context.add_init_script("""(() => {
                                  Object.defineProperty(navigator,'standalone',{get:()=>true});
                                  const media=window.matchMedia.bind(window);
                                  window.matchMedia=query=>query==='(display-mode: standalone)'?
                                    {matches:true,media:query,addListener(){},removeListener(){},addEventListener(){},removeEventListener(){}}:media(query);
                                })()""")
                            label = f"{width}-{role}-{'app-shell' if standalone else 'browser'}-{'large' if large else 'normal'}"
                            try:
                                page, errors = await page_fixture(context, role)
                                if large:
                                    await page.add_style_tag(content=LARGE_TEXT)

                                assert await page.evaluate("Boolean(navigator.standalone)") == standalone

                                async def check(state, page=page, label=label):
                                    for theme in ("light", "premium", "photo"):
                                        await page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                                        failures = await page.evaluate(CHECK_LAYOUT)
                                        if failures and screenshot_dir:
                                            await page.screenshot(path=str(screenshot_dir / "failure.png"), full_page=True)
                                        assert not failures, {"case": label, "theme": theme,
                                                              "state": state, "failures": failures}
                                    await page.evaluate("document.documentElement.dataset.theme='light'")

                                await check("idle")
                                await page.locator('[data-voice-action="record"]').click()
                                await page.locator('[data-voice-action="stop"]').wait_for(state="visible")
                                await page.wait_for_function("document.querySelector('[data-voice-level]').value>0")
                                await check("recording")
                                if screenshot_dir and width == 320 and role == "manager" and not standalone:
                                    await page.screenshot(path=str(screenshot_dir / f"{label}-recording.png"), full_page=True)
                                await page.locator('[data-voice-action="stop"]').click()
                                await page.locator('[data-voice-action="send"]').wait_for(state="visible")
                                await check("recorded-preview")
                                await page.locator('[data-voice-action="cancel"]').click()
                                await page.locator('[data-realtime-action="start"]').click()
                                await page.wait_for_function("peers.length&&peers.at(-1).connectionState==='connected'")
                                await page.evaluate("""() => {
                                  receive({type:'input_audio_buffer.committed',item_id:'layout-user'});
                                  receive({type:'conversation.item.input_audio_transcription.completed',item_id:'layout-user',transcript:'Как правильно объяснить гостю стоимость фотосессии? '+ 'ОченьДлинноеСлово'.repeat(12)});
                                  document.querySelector('.realtime-training details').open=true;
                                }""")
                                await check("live-and-transcript")
                                if screenshot_dir and width == 390 and role == "manager" and standalone and large:
                                    await page.screenshot(path=str(screenshot_dir / f"{label}-live.png"), full_page=True)
                                await page.locator('[data-realtime-action="mute"]').click()
                                await check("muted")
                                await page.locator('[data-realtime-action="stop"]').click()
                                await page.wait_for_function("document.querySelector('[data-realtime-action=start]').hidden===false")
                                await check("stopped")
                                assert not errors, {"case": label, "errors": errors}
                                results.append(label)
                            finally:
                                await context.close()
        finally:
            await browser.close()
    print(json.dumps({"ok": True, "layoutCases": len(results),
                      "states": ["idle", "recording", "recorded-preview", "live-and-transcript", "muted", "stopped"],
                      "widths": [320, 390, 768, 1440], "enlargedTextPercent": 150,
                      "themes": ["light", "premium", "photo"],
                      "standaloneShellIsSimulation": True}))


if __name__ == "__main__":
    asyncio.run(main())
