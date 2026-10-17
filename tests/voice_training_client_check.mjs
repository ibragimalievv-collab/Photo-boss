import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const listeners=new Map(),windowListeners=new Map(),events=[];
const status={textContent:''},player={hidden:true,pause(){},async play(){this.played=true;}},preview={hidden:true,dataset:{},pause(){},removeAttribute(){this.src='';}};
const controls=['listen','record','stop','send','cancel','finish'].map(action=>({dataset:{voiceAction:action},disabled:false,hidden:['stop','send','cancel'].includes(action),toggleAttribute(_,hide){this.hidden=hide;},closest(){return root;}}));
const root={dataset:{voiceSession:'1',voiceRevision:'1'},querySelector(selector){if(selector==='[data-voice-status]')return status;if(selector==='[data-voice-player]')return player;if(selector==='[data-voice-preview]')return preview;return controls.find(b=>selector.includes('"'+b.dataset.voiceAction+'"'))||null;},querySelectorAll(){return controls;}};
globalThis.document={documentElement:{dataset:{}},querySelector(){return root;},querySelectorAll(){return [];},addEventListener(name,fn){listeners.set(name,fn);},dispatchEvent(e){events.push(e);listeners.get(e.type)?.(e);return !e.defaultPrevented;}};
globalThis.window={addEventListener(name,fn){windowListeners.set(name,fn);}};
globalThis.CustomEvent=class {constructor(type,options={}){this.type=type;this.detail=options.detail;this.cancelable=options.cancelable;}preventDefault(){if(this.cancelable)this.defaultPrevented=true;}};
let stops=0,uploads=0,speeches=0,uploadFails=false,bytes=15,encoder='normal',permission='normal',captureResolve=null;
const nativeSetTimeout=globalThis.setTimeout;
globalThis.setTimeout=(callback,delay,...args)=>nativeSetTimeout(callback,permission==='timeout'&&delay===20000?0:delay,...args);
const streams=[],recorders=[];
function stream(){const track={readyState:'live',stop(){if(this.readyState!=='ended')stops++;this.readyState='ended';}};const value={getTracks(){return [track];}};streams.push(value);return value;}
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{async getUserMedia(){if(permission==='denied')throw Object.assign(Error('denied'),{name:'NotAllowedError'});if(['pending','timeout'].includes(permission))return new Promise(resolve=>{captureResolve=()=>resolve(stream());});return stream();}}}});
globalThis.MediaRecorder=class {static isTypeSupported(type){return encoder==='safari'?type==='audio/mp4;codecs=mp4a.40.2':true;}constructor(_,options){if(encoder==='constructor-fails')throw Error('encoder unavailable');if(encoder==='native-only'&&options)throw Error('native encoder only');this.mimeType=options?.mimeType||'audio/webm';this.state='inactive';recorders.push(this);}start(){if(encoder==='start-fails'){this.state='recording';throw Error('cannot start');}this.state='recording';}stop(){this.state='inactive';queueMicrotask(()=>{this.ondataavailable?.({data:new Blob([new Uint8Array(bytes)],{type:this.mimeType})});this.onstop?.();});}};
globalThis.voiceTestApi=async(path,options)=>{if(path.endsWith('/voice')){assert.equal(options.body.get('revision'),'1');assert.ok(options.body.get('audio').size>0);uploads++;if(uploadFails){uploadFails=false;throw Error('Network interrupted');}return {ok:true};}if(path.endsWith('/speech')){speeches++;return new Blob(['synthetic reply'],{type:'audio/mpeg'});}throw Error('Unexpected API request');};
let source=await readFile(new URL('../app/webapp/js/voice-training.js',import.meta.url),'utf8');
source=source.replace("import {api} from './api.js';","const api=globalThis.voiceTestApi;").replace("import {realtimeControls} from './realtime-training.js';","const realtimeControls=()=>'<section>Live fixture</section>';");
const module=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
assert.match(module.voiceControls({id:1,revision:1,status:'ACTIVE'}),/Ответ голосовым сообщением/);
assert.match(module.voiceControls({id:1,revision:1,status:'ACTIVE'}),/Live fixture/);
const button=action=>controls.find(b=>b.dataset.voiceAction===action);
const click=async action=>{await listeners.get('click')({target:{closest(){return button(action);}}});await new Promise(resolve=>setImmediate(resolve));};
await click('record');assert.equal(button('stop').hidden,false);assert.match(status.textContent,/Идёт запись/);
const liveStart=new CustomEvent('pb-live-voice-starting',{detail:{id:1},cancelable:true});document.dispatchEvent(liveStart);assert.equal(liveStart.defaultPrevented,true);
await click('stop');assert.equal(stops,1);assert.equal(button('send').hidden,false);
uploadFails=true;await click('send');assert.equal(uploads,1);assert.equal(button('send').hidden,false);assert.match(status.textContent,/доступна для повторной отправки/);
await click('send');assert.equal(uploads,2);assert.equal(speeches,1);assert.equal(player.played,true);assert.ok(events.some(e=>e.type==='pb-voice-turn-saved'));

// Safari MP4 and browsers that reject an explicitly chosen encoder remain usable.
for(const mode of ['safari','native-only']){encoder=mode;await click('record');assert.match(status.textContent,/Идёт запись/);await click('stop');assert.equal(button('send').hidden,false);await click('cancel');}
// Encoder construction/start failures must release devices and allow a fresh attempt.
for(const mode of ['constructor-fails','start-fails']){encoder=mode;await click('record');assert.match(status.textContent,/Не удалось запустить/);assert.ok(streams.at(-1).getTracks().every(t=>t.readyState==='ended'));encoder='normal';await click('record');assert.match(status.textContent,/Идёт запись/);await click('cancel');}
bytes=0;await click('record');await click('stop');assert.match(status.textContent,/пустую запись/);assert.equal(button('send').hidden,true);bytes=15;
permission='denied';await click('record');assert.match(status.textContent,/Нет доступа к микрофону/);
permission='timeout';await click('record');assert.match(status.textContent,/20 секунд/);captureResolve();await new Promise(resolve=>setImmediate(resolve));assert.ok(streams.at(-1).getTracks().every(t=>t.readyState==='ended'));

// A canceled permission prompt can resolve after a new recording has begun.
permission='pending';const pending=click('record');await new Promise(resolve=>setImmediate(resolve));assert.equal(button('cancel').disabled,false);await click('cancel');await pending;permission='normal';await click('record');const active=streams.at(-1);captureResolve();await new Promise(resolve=>setImmediate(resolve));assert.ok(active.getTracks().every(t=>t.readyState==='live'));assert.ok(streams.at(-1).getTracks().every(t=>t.readyState==='ended'));
await click('cancel');

// Late stop/error callbacks cannot release the next capture or resurrect a draft.
await click('record');const old=recorders.at(-1),oldStop=old.onstop,oldError=old.onerror;await click('cancel');await click('record');const current=streams.at(-1);oldStop();oldError();assert.ok(current.getTracks().every(t=>t.readyState==='live'));assert.equal(button('send').hidden,true);await click('cancel');
document.documentElement.dataset.pbLiveVoice='1';await click('record');assert.match(status.textContent,/Сначала завершите живой разговор/);delete document.documentElement.dataset.pbLiveVoice;
windowListeners.get('pagehide')();assert.ok(streams.every(s=>s.getTracks().every(t=>t.readyState==='ended')));
console.log('PASS: capture, Safari/native codecs, encoder recovery, retry, empty audio, late permission/events, mode exclusivity and device cleanup');
