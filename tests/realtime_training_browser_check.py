"""Exercise live Academy controls offline with Chromium's actual fake microphone.

Only the WebRTC peer/provider and application API are fixtures: no real AI call,
production request, credential, or human audio is used.
"""
import asyncio
import json
import math
import struct
import tempfile
import wave
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photo-boss-realtime-training.invalid"
FIXTURE = """<!doctype html><html lang="ru"><body><main id="fixture"></main>
<script type="module">
import {realtimeControls} from '/app/js/realtime-training.js';
window.streams=[];window.peers=[];window.sent=[];window.requests=[];
window.savedEvents=[];window.stopped=[];window.savedNotifications=[];
window.flags={denyMicrophone:false,failEvents:0,conflictEvents:0,loseStart:false,holdStart:false};
window.revision=1;window.sessionStatus='ACTIVE';window.currentLease=null;
window.startCount=0;window.statusChecks=0;window.finishCount=0;
const capture=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia=async options=>{
 if(flags.denyMicrophone)throw new DOMException('Fixture microphone denied','NotAllowedError');
 const stream=await capture(options);streams.push(stream);return stream;
};
class FixtureChannel extends EventTarget{
 constructor(){super();this.readyState='connecting';}
 send(data){if(this.readyState!=='open')throw Error('Fixture channel is not open');sent.push(JSON.parse(data));}
 close(){this.readyState='closed';}
 receive(event){this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(event)}));}
}
class FixturePeer{
 constructor(){this.connectionState='new';this.tracks=[];peers.push(this);}
 addTrack(track,stream){this.tracks.push({track,stream});}
 createDataChannel(label){this.channel=new FixtureChannel();this.label=label;return this.channel;}
 async createOffer(){return {type:'offer',sdp:'v=0\\r\\nfixture-offer'};}
 async setLocalDescription(offer){this.localDescription=offer;}
 async setRemoteDescription(answer){
  this.remoteDescription=answer;this.connectionState='connected';
  this.onconnectionstatechange?.();
  const first=this.tracks[0];document.querySelector('[data-realtime-player]').muted=true;
  this.ontrack?.({track:first.track,streams:[first.stream]});
  this.channel.readyState='open';this.channel.dispatchEvent(new Event('open'));
 }
 close(){this.connectionState='closed';}
}
window.RTCPeerConnection=FixturePeer;
window.receive=event=>peers.at(-1).channel.receive(event);
window.fixtureApi=async(path,options={})=>{
 requests.push({path,method:options.method||'GET',body:options.body||null});
 if(path==='/academy/development')return {sessions:[{id:1,revision,status:sessionStatus}]};
 if(path.endsWith('/realtime/start')){
  if(options.body.revision!==revision)throw Object.assign(Error('Fixture stale revision'),{status:409});
  startCount++;currentLease={leaseId:'fixture-lease-'+startCount,mode:options.body.mode};
  if(flags.holdStart)await new Promise(resolve=>{window.releaseStart=resolve;});
  if(flags.loseStart){flags.loseStart=false;throw Error('Fixture start response lost');}
  return {...currentLease,revision,sdp:'v=0\\r\\nfixture-answer',model:'fixture-realtime',maxSeconds:600};
 }
 if(path.endsWith('/realtime/status')){statusChecks++;return {active:Boolean(currentLease),revision,...currentLease};}
 if(path.endsWith('/realtime/stop')){
  stopped.push(options.body.leaseId);if(currentLease?.leaseId===options.body.leaseId)currentLease=null;
  return {ok:true};
 }
 if(path.endsWith('/realtime/events')){
  if(flags.failEvents>0){flags.failEvents--;throw Error('Fixture transcript network unavailable');}
  if(flags.conflictEvents>0){flags.conflictEvents--;revision++;throw Object.assign(Error('Fixture stale revision'),{status:409});}
  if(options.body.revision!==revision)throw Object.assign(Error('Fixture stale revision'),{status:409});
  let added=0;
  for(const entry of options.body.events){
   const old=savedEvents.find(item=>item.id===entry.id);
   if(old&&JSON.stringify(old)!==JSON.stringify(entry))throw Error('Fixture event changed on retry');
   if(!old){savedEvents.push(entry);added++;}
  }
  if(added)revision++;
  return {ok:true,revision,saved:added};
 }
 if(path.endsWith('/realtime/finish')){
  if(currentLease)throw Error('Fixture finish while live');
  if(options.body.revision!==revision)throw Error('Fixture stale finish revision');
  finishCount++;sessionStatus='COMPLETED';revision++;return {ok:true,revision};
 }
 throw Error('Unexpected fixture API path '+path);
};
document.addEventListener('pb-voice-turn-saved',event=>savedNotifications.push(event.detail.id));
window.renderFixture=()=>{document.querySelector('#fixture').innerHTML=realtimeControls({id:1,revision,status:sessionStatus});};
renderFixture();window.fixtureReady=true;
</script></body></html>"""


async def fixture_page(context):
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def route(request):
        path = request.request.url.removeprefix(BASE).split("?", 1)[0]
        if path == "/":
            return await request.fulfill(content_type="text/html", body=FIXTURE)
        if path in {"/app/js/realtime-training.js", "/app/js/domain.js"}:
            return await request.fulfill(content_type="text/javascript",
                                         body=(ROOT / "app/webapp/js" / path.rsplit("/", 1)[1]).read_text())
        if path == "/app/js/api.js":
            return await request.fulfill(content_type="text/javascript",
                                         body="export const api=(...args)=>window.fixtureApi(...args);")
        raise AssertionError(f"Unexpected request: {request.request.url}")

    await page.route("**/*", route)
    await page.goto(BASE)
    await page.wait_for_function("window.fixtureReady")
    return page, errors


async def connected(page, mode="coach"):
    await page.locator("[data-realtime-mode]").select_option(mode)
    await page.locator('[data-realtime-action="start"]').click()
    await page.wait_for_function("peers.length&&peers.at(-1).connectionState==='connected'&&sent.some(x=>x.type==='response.create')")
    assert await page.evaluate("streams.at(-1).getAudioTracks().length===1&&streams.at(-1).getAudioTracks()[0].readyState==='live'")
    assert await page.locator("[data-realtime-mode]").is_disabled()


async def main():
    with tempfile.NamedTemporaryFile(suffix=".wav") as capture_file:
        await check_browser(capture_file.name)


async def check_browser(capture_path):
    results = {}
    async with async_playwright() as playwright:
        samples = [int(7000 * math.sin(2 * math.pi * 180 * i / 48000)
                       + 3500 * math.sin(2 * math.pi * 337 * i / 48000)) for i in range(96000)]
        with wave.open(capture_path, "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            audio.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        browser = await playwright.chromium.launch(args=[
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
            f"--use-file-for-fake-audio-capture={capture_path}",
        ])
        context = await browser.new_context(permissions=["microphone"])
        try:
            page, errors = await fixture_page(context)
            try:
                await connected(page)
                await page.wait_for_function("document.querySelector('[data-realtime-meter]').textContent.includes('слышит ваш голос')")
                assert await page.evaluate("requests.find(x=>x.path.endsWith('/realtime/start')).body.mode==='coach'")
                assert "ИИ-наставником Photo Boss" in await page.evaluate("sent[0].response.instructions")
                await page.evaluate("window.playingAudio=document.querySelector('[data-realtime-player]');renderFixture()")
                await page.wait_for_function("document.querySelector('[data-realtime-player]')===playingAudio")
                await page.locator('[data-realtime-action="mute"]').click()
                assert await page.evaluate("!streams.at(-1).getAudioTracks()[0].enabled")
                assert await page.locator('[data-realtime-action="mute"]').inner_text() == "Включить микрофон"
                await page.locator('[data-realtime-action="mute"]').click()
                assert await page.evaluate("streams.at(-1).getAudioTracks()[0].enabled")
                await page.evaluate("""() => {
                    flags.failEvents=1;
                    receive({type:'input_audio_buffer.committed',item_id:'user_1'});
                    receive({type:'conversation.item.input_audio_transcription.completed',item_id:'user_1',transcript:'Как объяснить цену <script>?' });
                    receive({type:'response.created',response:{id:'response_1'}});
                    receive({type:'conversation.item.added',previous_item_id:'user_1',item:{type:'message',id:'ai_1',role:'assistant'}});
                    receive({type:'response.output_audio_transcript.delta',response_id:'response_1',item_id:'ai_1',delta:'Кадр стоит '});
                    receive({type:'response.output_audio_transcript.done',response_id:'response_1',item_id:'ai_1',transcript:'Кадр стоит 400 ₽.'});
                    receive({type:'response.done',response:{id:'response_1',status:'completed',output:[]}});
                    receive({type:'output_audio_buffer.started',response_id:'response_1'});
                    receive({type:'output_audio_buffer.stopped',response_id:'response_1'});
                }""")
                await page.wait_for_function("JSON.parse(sessionStorage.getItem('pb_live_training_1')).items.length===2")
                await page.wait_for_timeout(250)
                assert await page.evaluate("requests.every(x=>!x.path.endsWith('/realtime/events'))")
                assert not await page.locator("[data-realtime-transcript] script").count()
                assert "<script>" in await page.locator("[data-realtime-transcript]").text_content()
                await page.locator('[data-realtime-action="stop"]').click()
                await page.wait_for_function("stopped.length===1&&streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                await page.locator('[data-realtime-action="retry"]').wait_for(state="visible")
                assert "Есть несохранённые реплики" in await page.locator("[data-realtime-status]").inner_text()
                await page.evaluate("flags.conflictEvents=1;renderFixture()")
                await page.locator('[data-realtime-action="retry"]').click()
                await page.wait_for_function("savedEvents.length===2&&document.querySelector('[data-realtime-action=retry]').hidden")
                assert await page.evaluate("savedEvents.map(x=>x.role).join(',')==='employee,coach'")
                assert await page.evaluate("savedEvents[1].text==='Кадр стоит 400 ₽.'")
                assert await page.evaluate("Number(document.querySelector('[data-realtime-session]').dataset.realtimeRevision)===revision")
                await page.wait_for_function("!document.querySelector('[data-realtime-action=start]').hidden")
                assert await page.evaluate("peers[0].connectionState==='closed'&&peers[0].channel.readyState==='closed'")
                assert await page.evaluate("!document.documentElement.dataset.pbLiveVoice")
                await page.locator('[data-realtime-action="finish"]').click()
                await page.wait_for_function("finishCount===1&&sessionStorage.getItem('pb_live_training_1')===null")
                assert not errors, errors
                results.update({"nativeFakeMicrophone": True, "coachMode": True,
                                "rerenderKeepsLiveAudio": True, "muteUnmute": True,
                                "escapedOrderedTranscript": True, "networkAndRevisionRetry": True,
                                "stopReleasesMicrophone": True, "savedConversationFinish": True})
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                # A browser reload may occur before pagehide or the final save.
                await page.evaluate("""() => {
                    sessionStorage.setItem('pb_live_training_1',JSON.stringify({
                        id:1,revision:1,leaseId:'fixture-recovered',mode:'coach',closed:false,
                        items:[
                            {id:'user_done',role:'employee',text:'Как объяснить цену?',done:true,saved:false},
                            {id:'user_incomplete',role:'employee',text:'Частичная фраза',done:false,saved:false},
                            {id:'ai_unheard',role:'coach',text:'Невоспроизведённый текст',done:true,saved:false,played:false},
                            {id:'ai_heard',role:'coach',text:'Кадр стоит 400 ₽.',done:true,saved:false,played:true},
                        ]
                    }));
                }""")
                await page.reload()
                await page.wait_for_function("window.fixtureReady")
                await page.locator('[data-realtime-action="retry"]').click()
                await page.wait_for_function("savedEvents.length===3")
                assert await page.evaluate("""() => {
                    const marker='[Ответ ИИ прерван; неуслышанная часть исключена из разбора.]';
                    return savedEvents.map(x=>x.id).join(',')==='user_done,ai_unheard,ai_heard'
                        &&savedEvents[1].text===marker&&savedEvents[2].text==='Кадр стоит 400 ₽.'
                        &&JSON.parse(sessionStorage.getItem('pb_live_training_1')).missing===true;
                }""")
                assert not errors, errors
                results["reloadSealsUnheardAndIncompleteTranscript"] = True
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                await connected(page, "guest")
                await page.evaluate("""() => {
                    receive({type:'input_audio_buffer.committed',item_id:'user_1'});
                    receive({type:'conversation.item.input_audio_transcription.completed',item_id:'user_1',transcript:'Сколько стоит кадр?'});
                    receive({type:'response.created',response:{id:'response_cut'}});
                    receive({type:'conversation.item.added',item:{type:'message',id:'ai_cut',role:'assistant'},previous_item_id:'user_1'});
                    receive({type:'output_audio_buffer.started',response_id:'response_cut'});
                    receive({type:'response.output_audio_transcript.done',response_id:'response_cut',item_id:'ai_cut',transcript:'Этот полный ответ клиент ещё не услышал.'});
                    receive({type:'response.done',response:{id:'response_cut',status:'completed',output:[]}});
                }""")
                await page.wait_for_timeout(250)
                assert await page.evaluate("requests.every(x=>!x.path.endsWith('/realtime/events'))")
                await page.evaluate("""() => {
                    receive({type:'input_audio_buffer.speech_started'});
                    receive({type:'conversation.item.truncated',item_id:'ai_cut',audio_end_ms:25});
                    receive({type:'input_audio_buffer.committed',item_id:'user_2',previous_item_id:'ai_cut'});
                    receive({type:'input_audio_buffer.speech_stopped'});
                    receive({type:'conversation.item.input_audio_transcription.completed',item_id:'user_2',transcript:'Подождите, другой вопрос.'});
                    receive({type:'response.output_audio_transcript.done',response_id:'response_cut',item_id:'ai_cut',transcript:'Поздняя неуслышанная расшифровка.'});
                    receive({type:'response.done',response:{id:'response_cut',status:'cancelled',output:[{type:'message',id:'ai_cut',content:[{transcript:'Полный отменённый ответ.'}]}]}});
                    receive({type:'response.created',response:{id:'response_stop'}});
                    receive({type:'conversation.item.added',item:{type:'message',id:'ai_stop',role:'assistant'},previous_item_id:'user_2'});
                    receive({type:'output_audio_buffer.started',response_id:'response_stop'});
                    receive({type:'response.output_audio_transcript.done',response_id:'response_stop',item_id:'ai_stop',transcript:'Останавливаемый ответ не должен попасть в оценку.'});
                }""")
                await page.locator('[data-realtime-action="stop"]').click()
                await page.wait_for_function("stopped.length===1&&savedEvents.length===4")
                assert await page.evaluate("""() => {
                    const marker='[Ответ ИИ прерван; неуслышанная часть исключена из разбора.]';
                    return savedEvents[1].text===marker&&savedEvents[3].text===marker
                        &&savedEvents.map(x=>x.role).join(',')==='employee,client,employee,client'
                        &&sent.some(x=>x.type==='response.cancel');
                }""")
                assert await page.evaluate("streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                assert not errors, errors
                results.update({"interruptedSpeechExcludedFromHistory": True,
                                "lateCancelledTranscriptCannotOverwriteMarker": True,
                                "stopCancelsUnheardResponse": True})
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                await page.evaluate("flags.holdStart=true")
                await page.locator('[data-realtime-action="start"]').click()
                await page.wait_for_function("typeof releaseStart==='function'")
                await page.evaluate("history.replaceState(null,'','#home');document.querySelector('#fixture').innerHTML='Другая страница';")
                await page.wait_for_function("streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                await page.evaluate("releaseStart()")
                await page.wait_for_function("stopped.length===1&&currentLease===null")
                assert await page.evaluate("peers[0].connectionState==='closed'&&!peers[0].remoteDescription")
                assert not errors, errors
                results["navigationDuringStartClosesLateLease"] = True
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                await connected(page, "guest")
                assert "в роли гостя" in await page.evaluate("sent[0].response.instructions")
                # The real application uses replaceState rather than hashchange.
                await page.evaluate("history.replaceState(null,'','#home');document.querySelector('#fixture').innerHTML='Другая страница';")
                await page.wait_for_function("stopped.length===1&&streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                assert await page.evaluate("peers[0].connectionState==='closed'")
                assert not errors, errors
                results.update({"guestMode": True, "replaceStateNavigationReleasesMicrophone": True})
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                await page.evaluate("flags.loseStart=true")
                await page.locator('[data-realtime-action="start"]').click()
                await page.wait_for_function("statusChecks===1&&stopped.length===1&&!document.querySelector('[data-realtime-action=start]').hidden")
                assert await page.evaluate("streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))&&currentLease===null")
                assert "Fixture start response lost" in await page.locator("[data-realtime-status]").inner_text()
                await connected(page)
                await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                await page.wait_for_function("currentLease===null&&streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                assert not errors, errors
                results.update({"lostStartResponseRecoversLease": True, "pagehideReleasesMicrophone": True})
            finally:
                await page.close()

            page, errors = await fixture_page(context)
            try:
                await page.evaluate("flags.denyMicrophone=true")
                await page.locator('[data-realtime-action="start"]').click()
                await page.wait_for_function("document.querySelector('[data-realtime-status]').textContent.includes('Микрофон запрещён')")
                assert await page.evaluate("startCount===0&&peers.length===0&&!document.documentElement.dataset.pbLiveVoice")
                assert await page.locator('[data-realtime-action="start"]').is_visible()
                await page.evaluate("flags.denyMicrophone=false")
                await connected(page)
                await page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                await page.wait_for_function("currentLease===null&&streams.every(s=>s.getTracks().every(t=>t.readyState==='ended'))")
                assert not errors, errors
                results["deniedMicrophoneCanRetry"] = True
            finally:
                await page.close()

            print(json.dumps({"ok": True, **results}))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
