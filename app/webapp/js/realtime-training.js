import {api} from './api.js';
import {esc} from './domain.js';

let active=null,generation=0;
const interruptedText='[Ответ ИИ прерван; неуслышанная часть исключена из разбора.]';
const recovery=new Map();
const host=id=>document.querySelector(`.realtime-training[data-realtime-session="${Number(id)}"]`);
const key=id=>`pb_live_training_${Number(id)}`;
const notice=(id,text)=>{const node=host(id)?.querySelector('[data-realtime-status]');if(node)node.textContent=text;};
function remember(s){
 const value={id:s.id,revision:s.revision,leaseId:s.leaseId,mode:s.mode,items:s.items,missing:s.missing||false,closed:!!s.closed};
 recovery.set(s.id,value);
 try{sessionStorage.setItem(key(s.id),JSON.stringify(value));}catch{}
}
function recovered(id){
 if(recovery.has(id))return recovery.get(id);
 try{const value=JSON.parse(sessionStorage.getItem(key(id))||'null');if(value?.id===id&&typeof value.leaseId==='string'&&Array.isArray(value.items)&&value.items.length<=80){value.closed=true;sealTranscript(value);recovery.set(id,value);return value;}}catch{}
 return null;
}
function forget(id){recovery.delete(id);try{sessionStorage.removeItem(key(id));}catch{}}
function pending(s){return s.items.some(i=>i.done&&i.text&&!i.saved);}
function controls(s){
 const root=host(s.id);if(!root)return;
 const on=active===s&&!s.closed;
 for(const button of root.querySelectorAll('[data-realtime-action]')){
  const action=button.dataset.realtimeAction;
  button.hidden=action==='start'?on:action==='mute'||action==='sound'? !on:action==='stop'?!on:action==='retry'?(on||!pending(s)):false;
  button.disabled=action==='finish'?(on||s.finishing):action==='start'?s.finishing:false;
  if(action==='mute'){const label=s.muted?'Включить микрофон':'Выключить микрофон';if(button.textContent!==label)button.textContent=label;}
 }
 const selector=root.querySelector('[data-realtime-mode]');if(selector){selector.disabled=on||pending(s);selector.value=s.mode;}
 if(on&&s.audio&&root.querySelector('[data-realtime-player]')!==s.audio){const old=root.querySelector('[data-realtime-player]');if(old)old.replaceWith(s.audio);}
}
export function realtimeControls(session){
 if(session.status!=='ACTIVE')return '';
 const id=Number(session.id),s=active?.id===id?active:recovered(id);
 const mode=s?.mode||'coach';
 return `<section class="notice section realtime-training" data-realtime-session="${id}" data-realtime-revision="${Number(session.revision)}"><h3>Живой разговор с ИИ Photo Boss</h3><p>Говорите обычным голосом: ИИ услышит паузу и ответит. Можно перебивать. Собеседник — искусственный интеллект; текст разговора сохраняется в истории обучения после остановки.</p><label class="field">С кем говорить<select class="input" data-realtime-mode><option value="coach" ${mode==='coach'?'selected':''}>ИИ-наставник: объясняет и помогает</option><option value="guest" ${mode==='guest'?'selected':''}>Гость: тренировка диалога</option></select></label><div class="row wrap section"><button type="button" class="btn primary" data-realtime-action="start">Начать живой разговор</button><button type="button" class="btn ghost" data-realtime-action="mute" hidden>Выключить микрофон</button><button type="button" class="btn ghost" data-realtime-action="sound" hidden>Включить звук ИИ</button><button type="button" class="btn ghost" data-realtime-action="stop" hidden>Остановить разговор</button><button type="button" class="btn ghost" data-realtime-action="retry" ${s?.items?.some(i=>i.done&&i.text&&!i.saved)?'':'hidden'}>Сохранить реплики повторно</button><button type="button" class="btn primary" data-realtime-action="finish">Получить разбор разговора</button></div><p role="status" data-realtime-status>${s?.items?.some(i=>i.done&&i.text&&!i.saved)?'Есть несохранённые реплики. Повторите сохранение перед продолжением.':'Разрешите микрофон после нажатия. Один разговор — до 10 минут.'}</p><p class="small" data-realtime-meter>Микрофон выключен</p><p class="small" data-realtime-clock></p><audio data-realtime-player controls hidden></audio><details><summary>Текст живого разговора</summary><div data-realtime-transcript>${s?transcriptHTML(s):''}</div></details><p class="small">Нужен интернет. Если микрофон недоступен внутри Telegram, откройте этот же Photo Boss в Safari или Chrome и разрешите микрофон для сайта.</p></section>`;
}
function transcriptHTML(s){return s.items.filter(i=>i.text).map(i=>`<p><strong>${i.role==='employee'?'Вы':i.role==='coach'?'ИИ-наставник':'ИИ-гость'}:</strong> ${esc(i.text)}${i.saved?'':' <small>пока в этой вкладке</small>'}</p>`).join('');}
function renderTranscript(s){const node=host(s.id)?.querySelector('[data-realtime-transcript]');if(node)node.innerHTML=transcriptHTML(s);}
function item(s,id,role,previous){
 let value=s.items.find(i=>i.id===id);if(value)return value;
 if(!id||s.items.length>=80)return null;
 value={id,role,text:'',done:false,saved:false};
 const index=previous?s.items.findIndex(i=>i.id===previous):-1;
 if(index>=0)s.items.splice(index+1,0,value);else s.items.push(value);
 return value;
}
async function refreshRevision(s){
 const data=await api('/academy/development');const current=data.sessions.find(x=>x.id===s.id);
 if(!current||current.status!=='ACTIVE')throw Error('Тренировка уже завершена. Откройте её историю.');
 s.revision=current.revision;
}
async function flush(s){
 // Generated text can arrive before playback. Keep it recoverable in this tab
 // until stop, when interruption events can no longer alter immutable history.
 if(active===s&&!s.closed)return;
 if(s.flushing)return s.flushing;
 s.flushing=(async()=>{
  while(true){
   const batch=[];
   for(const value of s.items){
    if(value.saved)continue;
    if(!value.done)break;
    if(value.text)batch.push({id:value.id,role:value.role,text:value.text.slice(0,2000)});
    if(batch.length===8)break;
   }
   if(!batch.length)break;
   const body={leaseId:s.leaseId,revision:s.revision,events:batch};
   let response;
   try{response=await api(`/academy/sales-training/${s.id}/realtime/events`,{method:'POST',body,timeoutMs:30000});}
   catch(error){if(error.status!==409)throw error;await refreshRevision(s);body.revision=s.revision;response=await api(`/academy/sales-training/${s.id}/realtime/events`,{method:'POST',body,timeoutMs:30000});}
   s.revision=response.revision;
   const liveRoot=host(s.id);if(liveRoot)liveRoot.dataset.realtimeRevision=String(s.revision);
   const recordedRoot=document.querySelector(`.voice-training[data-voice-session="${s.id}"]`);if(recordedRoot)recordedRoot.dataset.voiceRevision=String(s.revision);
   for(const entry of batch)s.items.find(i=>i.id===entry.id).saved=true;
   remember(s);renderTranscript(s);controls(s);
  }
 })();
 try{await s.flushing;}finally{s.flushing=null;}
}
function saveLater(s){
 remember(s);renderTranscript(s);clearTimeout(s.saveTimer);
 s.saveTimer=setTimeout(()=>flush(s).catch(error=>{notice(s.id,error.message+' Реплики остаются в этой вкладке: нажмите «Сохранить реплики повторно».');controls(s);}),150);
}
function interrupted(value){
 if(!value||value.role==='employee')return;
 value.interrupted=true;value.text=interruptedText;value.done=true;
}
function interruptResponse(s,responseId){
 if(!responseId)return;
 for(const value of s.items)if(value.responseId===responseId)interrupted(value);
}
function sealTranscript(s){
 for(const value of s.items){
  if(value.saved)continue;
  if(value.role!=='employee'&&!value.played)interrupted(value);
  if(!value.done){value.done=true;if(value.role==='employee'){value.saved=true;s.missing=true;}else if(!value.text)value.saved=true;}
 }
}
function handleEvent(s,event){
 if(s.closed)return;
 const assistant=s.mode==='coach'?'coach':'client';
 if(event.type==='conversation.item.added'||event.type==='conversation.item.created'){
  const value=event.item;
  if(value?.type==='message'&&['user','assistant'].includes(value.role))item(s,value.id,value.role==='user'?'employee':assistant,event.previous_item_id);
 }
 if(event.type==='input_audio_buffer.committed')item(s,event.item_id,'employee',event.previous_item_id);
 if(event.type==='conversation.item.input_audio_transcription.completed'){
  const value=item(s,event.item_id,'employee');if(value){value.text=String(event.transcript||'').trim().slice(0,2000);value.done=true;saveLater(s);}
 }
 if(event.type==='conversation.item.input_audio_transcription.failed'){
  const value=item(s,event.item_id,'employee');if(value){value.done=true;value.saved=true;}
  notice(s.id,'Не удалось распознать одну реплику. Повторите её; эта реплика не попадёт в разбор.');saveLater(s);
 }
 if(['response.output_audio_transcript.delta','response.audio_transcript.delta'].includes(event.type)){
  const value=item(s,event.item_id,assistant);if(value){value.responseId=event.response_id||value.responseId||s.responseId;if(!value.interrupted)value.text=(value.text+String(event.delta||'')).slice(0,2000);renderTranscript(s);}
 }
 if(['response.output_audio_transcript.done','response.audio_transcript.done'].includes(event.type)){
  const value=item(s,event.item_id,assistant);if(value){value.responseId=event.response_id||value.responseId||s.responseId;if(!value.interrupted)value.text=String(event.transcript||value.text).trim().slice(0,2000);value.done=true;saveLater(s);}
 }
 if(event.type==='response.done'){
  s.responding=false;
  for(const output of event.response?.output||[]){if(output.type!=='message')continue;const value=item(s,output.id,assistant);if(value){value.responseId=event.response.id||value.responseId||s.responseId;const text=(output.content||[]).map(x=>x.transcript||x.text||'').join('');if(!value.interrupted)value.text=(text||value.text).slice(0,2000);value.done=true;if(event.response.status==='cancelled')interrupted(value);}}
  if(event.response?.status==='cancelled')interruptResponse(s,event.response.id||s.responseId);
  saveLater(s);
  if(event.response?.status==='failed')notice(s.id,'ИИ не смог ответить. Остановите разговор и подключитесь повторно; сохранённые реплики останутся в истории.');
 }
 if(event.type==='response.created'){s.responding=true;s.responseId=event.response?.id;}
 if(event.type==='conversation.item.truncated'){interrupted(item(s,event.item_id,assistant));saveLater(s);}
 if(event.type==='input_audio_buffer.speech_started'){s.speaking=true;interruptResponse(s,s.playingResponse);notice(s.id,'Слушаю вас…');saveLater(s);}
 if(event.type==='input_audio_buffer.speech_stopped'){s.speaking=false;notice(s.id,'ИИ готовит ответ…');}
 if(event.type==='output_audio_buffer.started'){s.playingResponse=event.response_id||s.responseId;notice(s.id,'ИИ говорит. Можно перебить или задать следующий вопрос.');}
 if(event.type==='output_audio_buffer.cleared'){interruptResponse(s,event.response_id||s.playingResponse);s.playingResponse=null;saveLater(s);}
 if(event.type==='output_audio_buffer.stopped'){const responseId=event.response_id||s.playingResponse;for(const value of s.items)if(responseId&&value.responseId===responseId)value.played=true;s.playingResponse=null;notice(s.id,'Ваш ход. Говорите обычным голосом.');}
 if(event.type==='error')notice(s.id,'Сбой голосового сервиса. Остановите разговор и подключитесь повторно.');
 if(s.items.length>=78)void stop(s,'Достигнут объём одной тренировки. Сохраните разговор и получите разбор.');
}
function meter(s){
 try{
  const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)return;
  s.context=new Audio();void s.context.resume().catch(()=>{});
  const source=s.context.createMediaStreamSource(s.stream),analyser=s.context.createAnalyser();analyser.fftSize=256;source.connect(analyser);
  const samples=new Uint8Array(analyser.fftSize);
  s.meterTimer=setInterval(()=>{analyser.getByteTimeDomainData(samples);const level=Math.sqrt(samples.reduce((sum,x)=>sum+(x-128)**2,0)/samples.length);const node=host(s.id)?.querySelector('[data-realtime-meter]');if(node)node.textContent=s.muted?'Микрофон выключен':level>2?'Микрофон слышит ваш голос':'Микрофон включён · ожидает речь';},250);
 }catch{}
}
function release(s){
 clearTimeout(s.saveTimer);clearTimeout(s.limitTimer);clearTimeout(s.connectTimer);clearTimeout(s.disconnectTimer);clearInterval(s.clockTimer);clearInterval(s.meterTimer);
 for(const track of s.stream?.getTracks()||[])track.stop();s.stream=null;
 try{s.channel?.close();s.pc?.close();}catch{}
 try{s.audio?.pause();if(s.audio)s.audio.srcObject=null;}catch{}
 if(s.context)void s.context.close().catch(()=>{});
 if(document.documentElement.dataset.pbLiveVoice===String(s.id))delete document.documentElement.dataset.pbLiveVoice;
}
async function endLease(s,keepalive=false){
 if(!s.leaseId)return;
 await api(`/academy/sales-training/${s.id}/realtime/stop`,{method:'POST',body:{leaseId:s.leaseId},timeoutMs:15000,keepalive});
}
async function stop(s,message='Разговор остановлен. Реплики сохранены; можно продолжить или получить разбор.',unloading=false){
 if(s.stopping)return s.stopping;
 s.stopping=(async()=>{
  s.ending=true;
  for(const track of s.stream?.getAudioTracks()||[])track.enabled=false;
  s.audio?.pause();
  if(!s.closed&&!unloading){
   notice(s.id,'Останавливаем микрофон и сохраняем последние реплики…');
   if(s.responding&&s.channel?.readyState==='open')s.channel.send(JSON.stringify({type:'response.cancel'}));
   const deadline=Date.now()+4000;
   // Give the provider time to finish the transcript of the last spoken turn.
   await new Promise(resolve=>setTimeout(resolve,450));
   while(Date.now()<deadline&&(s.speaking||s.items.some(i=>i.role==='employee'&&!i.done)))await new Promise(resolve=>setTimeout(resolve,100));
  }
  s.closed=true;release(s);if(active===s)active=null;
  sealTranscript(s);
  remember(s);controls(s);
  let failure='';
  try{await flush(s);}catch(error){failure=error.message+' Есть несохранённые реплики — повторите сохранение.';}
  try{await endLease(s,unloading);}catch{failure+=' Соединение закроется автоматически; повторное подключение может потребовать ожидания.';}
  notice(s.id,(failure||message)+(s.missing?' Последняя реплика не распознана полностью и не включена в разбор.':''));renderTranscript(s);controls(s);
  remember(s);
  if(!unloading)document.dispatchEvent(new CustomEvent('pb-voice-turn-saved',{detail:{id:s.id}}));
 })();
 try{await s.stopping;}finally{s.stopping=null;}
}
function microphoneError(error){
 if(error.name==='NotAllowedError'||error.name==='SecurityError')return 'Микрофон запрещён. Разрешите микрофон для Photo Boss в настройках браузера. В Telegram Web откройте приложение в Safari или Chrome.';
 if(error.name==='NotFoundError')return 'Микрофон не найден. Подключите микрофон и повторите.';
 if(error.name==='NotReadableError')return 'Микрофон занят другим приложением. Завершите другой звонок или запись и повторите.';
 return error.message||'Не удалось подключить микрофон.';
}
async function start(root){
 const id=Number(root.dataset.realtimeSession),old=recovered(id);
 if(active)throw Error('Сначала остановите текущий живой разговор.');
 if(old?.items?.some(i=>!i.saved&&i.text))throw Error('Сначала сохраните оставшиеся реплики предыдущего разговора.');
 const ready=new CustomEvent('pb-live-voice-starting',{detail:{id},cancelable:true});if(!document.dispatchEvent(ready))throw Error('Сначала отправьте или удалите записанный голосовой ответ.');
 if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||typeof RTCPeerConnection==='undefined')throw Error('Живой разговор требует HTTPS и браузер с микрофоном. Откройте Photo Boss в Safari или Chrome.');
 const ticket=++generation,s={id,revision:Number(root.dataset.realtimeRevision),mode:root.querySelector('[data-realtime-mode]').value,items:[],closed:false,muted:false,leaseId:null};
 active=s;document.documentElement.dataset.pbLiveVoice=String(id);controls(s);notice(id,'Разрешите микрофон. Подключаем живого ИИ…');
 let timeout;
 try{
  const granted=navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
  granted.then(stream=>{if(s.closed||ticket!==generation)for(const track of stream.getTracks())track.stop();},()=>{});
  s.stream=await Promise.race([granted,new Promise((_,reject)=>{timeout=setTimeout(()=>reject(Error('Ожидание микрофона истекло. Проверьте разрешение сайта и нажмите ещё раз.')),20000);})]);clearTimeout(timeout);
  if(s.closed||ticket!==generation)return;
  if(!host(id))throw Error('Тренировка закрыта. Откройте её снова.');
  s.audio=host(id).querySelector('[data-realtime-player]');s.audio.autoplay=true;s.audio.setAttribute('playsinline','');
  s.pc=new RTCPeerConnection();for(const track of s.stream.getAudioTracks())s.pc.addTrack(track,s.stream);
  s.pc.ontrack=event=>{if(s.closed)return;s.audio.srcObject=event.streams[0]||new MediaStream([event.track]);s.audio.hidden=false;void s.audio.play().catch(()=>notice(id,'Разговор подключён. Нажмите «Включить звук ИИ», чтобы слышать собеседника.'));};
  s.pc.onconnectionstatechange=()=>{
   if(s.closed)return;
   if(s.pc.connectionState==='connected'){clearTimeout(s.connectTimer);clearTimeout(s.disconnectTimer);notice(id,'Разговор подключён. Говорите — ИИ ответит после паузы.');}
   if(s.pc.connectionState==='failed')void stop(s,'Соединение прервалось. Проверьте интернет и нажмите «Начать живой разговор» повторно.');
   if(s.pc.connectionState==='disconnected'){notice(id,'Соединение прервалось. Пытаемся восстановить…');clearTimeout(s.disconnectTimer);s.disconnectTimer=setTimeout(()=>void stop(s,'Связь потеряна. Проверьте интернет и подключитесь повторно.'),8000);}
  };
  s.channel=s.pc.createDataChannel('oai-events');
  s.channel.addEventListener('message',event=>{try{handleEvent(s,JSON.parse(event.data));}catch{notice(id,'Не удалось прочитать событие разговора. Если ответ не слышен, подключитесь повторно.');}});
  s.channel.addEventListener('open',()=>{if(s.closed)return;s.channel.send(JSON.stringify({type:'response.create',response:{instructions:s.mode==='coach'?'Коротко представься ИИ-наставником Photo Boss и задай один вопрос: что сотрудник хочет отработать сегодня?':'Коротко начни диалог в роли гостя по выбранному сценарию. Дождись ответа сотрудника.'}}));});
  const offer=await s.pc.createOffer();await s.pc.setLocalDescription(offer);
  if(s.closed||ticket!==generation)return;
  if(old?.leaseId)await endLease(old);
  await refreshRevision(s);
  if(s.closed||ticket!==generation)return;
  s.requested=true;
  const response=await api(`/academy/sales-training/${id}/realtime/start`,{method:'POST',body:{revision:s.revision,sdp:offer.sdp,mode:s.mode},timeoutMs:60000});
  s.leaseId=response.leaseId;s.model=response.model;
  if(s.closed||ticket!==generation){await endLease(s);return;}
  remember(s);await s.pc.setRemoteDescription({type:'answer',sdp:response.sdp});
  meter(s);const began=Date.now();
  s.clockTimer=setInterval(()=>{const node=host(id)?.querySelector('[data-realtime-clock]');if(node)node.textContent=`${Math.floor((Date.now()-began)/60000)}:${String(Math.floor((Date.now()-began)/1000)%60).padStart(2,'0')} · ИИ Photo Boss`;},1000);
  s.limitTimer=setTimeout(()=>void stop(s,'Время разговора завершилось. Получите разбор или начните новый разговор.'),Math.min(Number(response.maxSeconds)||600,600)*1000);
  s.connectTimer=setTimeout(()=>{if(s.pc?.connectionState!=='connected')void stop(s,'Не удалось установить голосовую связь. Проверьте сеть и подключитесь повторно.');},25000);
  notice(id,s.pc.connectionState==='connected'?'Разговор подключён. Говорите обычным голосом.':'Устанавливаем голосовую связь…');controls(s);
 }catch(error){clearTimeout(timeout);s.closed=true;release(s);if(active===s)active=null;
  try{if(s.requested&&!s.leaseId&&!error.status){const state=await api(`/academy/sales-training/${id}/realtime/status`,{timeoutMs:15000});if(state.active)s.leaseId=state.leaseId;}if(s.leaseId){remember(s);await endLease(s);}}catch{}
  notice(id,microphoneError(error));controls(s);}
}
async function finish(root){
 const id=Number(root.dataset.realtimeSession);let s=active?.id===id?active:recovered(id);
 if(!s)throw Error('Сначала проведите живой разговор с ИИ. Для обычной записи используйте её кнопку завершения.');
 if(active===s)await stop(s);
 s.finishing=true;controls(s);notice(id,'Сохраняем разговор и готовим разбор…');
 try{
  await endLease(s);
  await flush(s);
  if(s.items.some(i=>!i.done))throw Error('Не все реплики распознаны. Повторите сохранение перед разбором.');
  await refreshRevision(s);
  await api(`/academy/sales-training/${id}/realtime/finish`,{method:'POST',body:{leaseId:s.leaseId,revision:s.revision},timeoutMs:100000});
  forget(id);notice(id,'Разбор сохранён.');document.dispatchEvent(new CustomEvent('pb-voice-turn-saved',{detail:{id}}));
 }finally{s.finishing=false;controls(s);}
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-realtime-action]');if(!button)return;
 const root=button.closest('.realtime-training'),id=Number(root.dataset.realtimeSession),action=button.dataset.realtimeAction;
 try{
  if(action==='start')return await start(root);
  if(action==='finish')return await finish(root);
  const s=active?.id===id?active:recovered(id);if(!s)throw Error('Сначала начните живой разговор.');
  if(action==='stop')return await stop(s);
  if(action==='retry'){await flush(s);notice(id,'Реплики сохранены.');controls(s);return;}
  if(action==='mute'){s.muted=!s.muted;for(const track of s.stream?.getAudioTracks()||[])track.enabled=!s.muted;controls(s);notice(id,s.muted?'Микрофон выключен. ИИ вас не слышит.':'Микрофон включён.');}
  if(action==='sound')await s.audio?.play();
 }catch(error){notice(id,microphoneError(error));}
});
function leave(){generation++;if(active){const s=active;if(s.ending){s.closed=true;release(s);}void stop(s,'Разговор остановлен при закрытии экрана.',true);}}
window.addEventListener('pagehide',leave);window.addEventListener('hashchange',leave);
document.addEventListener('pb-chat-account-reset',()=>{leave();for(const id of recovery.keys())forget(id);try{for(const name of Object.keys(sessionStorage))if(name.startsWith('pb_live_training_'))sessionStorage.removeItem(name);}catch{}});
document.addEventListener('visibilitychange',()=>{if(document.hidden)leave();});
new MutationObserver(()=>{if(active){if(!host(active.id))leave();else controls(active);}}).observe(document.body,{childList:true,subtree:true});
