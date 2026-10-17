import {api} from './api.js';
import {esc} from './domain.js';

let recorder=null,stream=null,timer=null,draft=null,busy=false,cancelled=false,audioURL=null,recording=null;
const host=id=>document.querySelector(`.voice-training[data-voice-session="${Number(id)}"]`);
function status(id,message){const node=host(id)?.querySelector('[data-voice-status]');if(node)node.textContent=message;}
function release(){clearTimeout(timer);timer=null;for(const track of stream?.getTracks()||[])track.stop();stream=null;}
function stopAll(){cancelled=true;if(recorder?.state==='recording')recorder.stop();release();recording=null;draft=null;busy=false;if(audioURL)URL.revokeObjectURL(audioURL);audioURL=null;document.querySelectorAll('[data-voice-player],[data-voice-preview]').forEach(a=>{a.pause();if(a.dataset.url)URL.revokeObjectURL(a.dataset.url);});}
window.addEventListener('hashchange',stopAll);window.addEventListener('pagehide',stopAll);document.addEventListener('pb-chat-account-reset',stopAll);

export function voiceControls(s){return `<section class="notice section voice-training" data-voice-session="${s.id}" data-voice-revision="${s.revision}"><h3>Голосовая тренировка с ИИ</h3><p>Слушайте гостя и отвечайте голосом. Собеседника озвучивает искусственный интеллект.</p><div class="row wrap"><button type="button" class="btn ghost" data-voice-action="listen">Прослушать ${s.status==='ACTIVE'?'гостя':'разбор'}</button>${s.status==='ACTIVE'?'<button type="button" class="btn primary" data-voice-action="record">Записать ответ</button><button type="button" class="btn ghost" data-voice-action="stop" hidden>Остановить запись</button><button type="button" class="btn primary" data-voice-action="send" hidden>Отправить голосовой ответ</button><button type="button" class="btn ghost" data-voice-action="cancel" hidden>Удалить запись</button><button type="button" class="btn ghost" data-voice-action="finish">Завершить и прослушать разбор</button>':''}</div><p role="status" data-voice-status>Для записи нажмите кнопку и разрешите микрофон. Одна реплика — до 90 секунд.</p><audio data-voice-player controls hidden></audio><audio data-voice-preview controls hidden></audio></section>`;}

function buttons(id){const root=host(id);if(!root)return;const isRecording=recording?.id===id,hasDraft=draft?.id===id;for(const b of root.querySelectorAll('button'))b.disabled=busy;root.querySelector('[data-voice-action="record"]')?.toggleAttribute('hidden',isRecording||hasDraft);root.querySelector('[data-voice-action="stop"]')?.toggleAttribute('hidden',!isRecording);root.querySelector('[data-voice-action="send"]')?.toggleAttribute('hidden',!hasDraft);root.querySelector('[data-voice-action="cancel"]')?.toggleAttribute('hidden',!hasDraft&&!isRecording);}
async function listen(id){status(id,'Готовим голосовой ответ…');const blob=await api(`/academy/sales-training/${id}/speech`,{method:'POST',body:{},responseType:'blob',timeoutMs:75000});const audio=host(id)?.querySelector('[data-voice-player]');if(!audio)return;if(audioURL)URL.revokeObjectURL(audioURL);audioURL=URL.createObjectURL(blob);audio.src=audioURL;audio.hidden=false;try{await audio.play();status(id,'Голосовой ответ готов.');}catch{status(id,'Нажмите «Воспроизвести» в плеере, чтобы прослушать ответ.');}}
async function record(id,revision){
 if(!navigator.mediaDevices?.getUserMedia||typeof MediaRecorder==='undefined')throw Error('Запись голоса недоступна в этом браузере. Откройте Photo Boss в Safari или Chrome с доступом к микрофону.');
 if(recording)throw Error('Сначала завершите текущую запись.');
 document.querySelectorAll('[data-voice-player]').forEach(a=>a.pause());
 cancelled=false;status(id,'Разрешите доступ к микрофону…');
 try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});}catch{throw Error('Нет доступа к микрофону. Разрешите микрофон в настройках браузера и повторите.');}
 if(!host(id)){release();return;}
 const mime=['audio/webm;codecs=opus','audio/mp4','audio/webm'].find(type=>MediaRecorder.isTypeSupported(type));
 if(!mime){release();throw Error('Браузер не поддерживает подходящий формат записи. Обновите браузер.');}
 let parts=[],size=0;recorder=new MediaRecorder(stream,{mimeType:mime});recording={id,revision};
 recorder.ondataavailable=e=>{if(e.data.size){parts.push(e.data);size+=e.data.size;if(size>10*1024*1024){cancelled=true;recorder.stop();status(id,'Запись превысила 10 МБ. Запишите более короткую реплику.');}}};
 recorder.onerror=()=>{cancelled=true;release();recording=null;buttons(id);status(id,'Не удалось записать голос. Повторите запись.');};
 recorder.onstop=()=>{release();recording=null;if(!cancelled&&parts.length){const blob=new Blob(parts,{type:mime});draft={id,revision,blob,mime:mime.split(';')[0]};const preview=host(id)?.querySelector('[data-voice-preview]');if(preview){if(preview.dataset.url)URL.revokeObjectURL(preview.dataset.url);preview.src=URL.createObjectURL(blob);preview.dataset.url=preview.src;preview.hidden=false;}status(id,'Ответ записан. Прослушайте его или нажмите «Отправить голосовой ответ».');}parts=[];buttons(id);};
 recorder.start(1000);timer=setTimeout(()=>{if(recorder?.state==='recording')recorder.stop();},90000);status(id,'Идёт запись. Говорите и нажмите «Остановить запись».');buttons(id);
}
function saved(id){document.dispatchEvent(new CustomEvent('pb-voice-turn-saved',{detail:{id}}));}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-voice-action]');if(!b)return;const root=b.closest('.voice-training'),id=Number(root.dataset.voiceSession),revision=Number(root.dataset.voiceRevision),action=b.dataset.voiceAction;if(busy)return;
 try{
  if(action==='record'){busy=true;buttons(id);await record(id,revision);return;}
  if(action==='stop'){if(recorder?.state==='recording')recorder.stop();return;}
  if(action==='cancel'){cancelled=true;if(recorder?.state==='recording')recorder.stop();release();recording=null;draft=null;const preview=root.querySelector('[data-voice-preview]');if(preview.dataset.url)URL.revokeObjectURL(preview.dataset.url);preview.hidden=true;preview.removeAttribute('src');buttons(id);status(id,'Запись удалена. Можно записать новый ответ.');return;}
  if(recording)throw Error('Сначала остановите запись.');busy=true;buttons(id);
  if(action==='listen')await listen(id);
  if(action==='send'){if(!draft||draft.id!==id||draft.revision!==revision)throw Error('Диалог уже изменился. Запишите новый ответ в текущей тренировке.');const form=new FormData();form.append('revision',String(draft.revision));form.append('audio',draft.blob,'voice.'+(draft.mime==='audio/mp4'?'mp4':'webm'));status(id,'Распознаём голос и готовим ответ гостя…');await api(`/academy/sales-training/${id}/voice`,{method:'POST',body:form,timeoutMs:180000});draft=null;saved(id);await listen(id);}
  if(action==='finish'){if(draft)throw Error('Сначала отправьте или удалите записанную реплику.');status(id,'ИИ готовит итоговый разбор…');await api(`/academy/sales-training/${id}/turn`,{method:'POST',body:{revision,text:'',finish:true},timeoutMs:90000});saved(id);await listen(id);}
 }catch(error){status(id,error.message+(draft?' Не закрывайте тренировку: запись доступна для повторной отправки.':''));}finally{busy=false;buttons(id);}
});
