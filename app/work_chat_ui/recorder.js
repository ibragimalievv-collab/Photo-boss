import {icon} from '/work-chat/ui.js';
import {cameraConstraints,openCamera} from '/work-chat/camera.js';
const panel=document.createElement('dialog');panel.className='pb-record-dialog';
panel.setAttribute('aria-labelledby','pbRecordTitle');document.body.append(panel);
let generation=0,stream=null,recorder=null,parts=[],blob=null,url=null,timer=null,started=0,bytes=0,mode='audio',sendFile=null,phase='closed',previousFocus=null,facing='user';
const MAX_BYTES=19*1024*1024;
function setPhase(value){phase=value;panel.dataset.phase=value;}
function release(){clearInterval(timer);timer=null;stream?.getTracks().forEach(t=>t.stop());stream=null;}
function cleanup(){generation++;if(recorder?.state==='recording')recorder.stop();recorder=null;release();parts=[];blob=null;if(url)URL.revokeObjectURL(url);url=null;}
function close(){setPhase('closed');cleanup();panel.close();previousFocus?.focus?.();}
function error(message){const el=panel.querySelector('[data-record-error]');if(el){el.hidden=false;el.textContent=message;}}
function stop(){if(recorder?.state==='recording')recorder.stop();}
function extension(type){return type.includes('mp4')?(mode==='audio'?'m4a':'mp4'):type.includes('ogg')?'ogg':mode==='audio'?'weba':'webm';}
function start(){
 if(!stream||!['ready','loading'].includes(phase))return;
 const ticket=generation,live=panel.querySelector('[data-record-live]');
 try{
  const types=mode==='audio'?['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus']:['video/webm;codecs=vp8,opus','video/mp4','video/webm'];
  const mimeType=types.find(type=>MediaRecorder.isTypeSupported(type));
  if(!mimeType)throw new Error('Устройство не поддерживает запись. Прикрепите готовый файл.');
  const r=new MediaRecorder(stream,{mimeType,audioBitsPerSecond:64000,...(mode==='video'?{videoBitsPerSecond:700000}:{})});
  recorder=r;parts=[];bytes=0;started=Date.now();setPhase('recording');
  panel.querySelector('[data-record-start]').hidden=true;panel.querySelector('[data-record-camera]').hidden=true;
  panel.querySelector('[data-record-error]').hidden=true;
  r.ondataavailable=e=>{if(ticket!==generation||!e.data.size)return;parts.push(e.data);bytes+=e.data.size;if(bytes>=MAX_BYTES)stop();};
  r.onerror=()=>{release();setPhase('error');error('Запись прервалась. Закройте окно и попробуйте ещё раз.');};
  r.onstop=()=>{
   if(ticket!==generation)return;
   release();setPhase('preview');live.srcObject=null;live.hidden=true;
   panel.querySelector('[data-record-stop]').hidden=true;
   blob=new Blob(parts,{type:r.mimeType});parts=[];
   if(!blob.size||blob.size>20*1024*1024){error('Запись не сохранилась или больше 20 МБ. Попробуйте записать короче.');return;}
   url=URL.createObjectURL(blob);const preview=panel.querySelector('[data-record-preview]');preview.src=url;preview.hidden=false;
   panel.querySelector('[data-record-send]').hidden=false;
   panel.querySelector('[data-record-state]').textContent='Запись готова. Проверьте её перед отправкой.';
  };
  const update=()=>{const seconds=Math.floor((Date.now()-started)/1000);panel.querySelector('[data-record-state]').textContent=`Идёт запись · ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;if(seconds>=(mode==='audio'?300:60))stop();};
  r.start(250);const button=panel.querySelector('[data-record-stop]');button.hidden=false;button.disabled=false;update();timer=setInterval(update,500);
 }catch(e){release();setPhase('error');error(e.message);}
}
export async function openRecorder(kind,onSend){
 if(phase!=='closed')return;
 cleanup();mode=kind;sendFile=onSend;facing='user';setPhase('loading');
 const ticket=generation;previousFocus=document.activeElement;
 panel.innerHTML=`<div class="pb-record-hero" aria-hidden="true">${icon(mode==='audio'?'mic':'video')}</div><h2 id="pbRecordTitle">${mode==='audio'?'Голосовое сообщение':'Видеосообщение'}</h2><p data-record-state>Ожидаем доступ ${mode==='audio'?'к микрофону':'к камере и микрофону'}…</p><video data-record-live autoplay playsinline muted ${mode==='audio'?'hidden':''}></video><${mode==='audio'?'audio':'video'} data-record-preview controls playsinline hidden></${mode==='audio'?'audio':'video'}><p data-record-error role="alert" hidden></p><div class="pb-record-actions"><button data-record-camera hidden aria-label="Включить заднюю камеру">${icon('flip')} Сменить камеру</button><button data-record-start hidden>${icon('video')} Начать запись</button><button data-record-stop disabled hidden>${icon('stop')} Остановить запись</button><button data-record-send hidden>${icon('send')} Отправить</button><button data-record-close>Отмена</button></div><p class="pb-record-note">${mode==='audio'?'До 5 минут':'До 1 минуты. Камеру можно выбрать до начала записи'}. Перед отправкой можно ${mode==='audio'?'прослушать':'посмотреть'} запись.</p>`;
 panel.showModal();
 try{
  if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw new Error('Запись недоступна в этой версии Telegram. Можно прикрепить готовый файл.');
  const acquired=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:mode==='video'?cameraConstraints():false});
  if(ticket!==generation){acquired.getTracks().forEach(t=>t.stop());return;}
  stream=acquired;const live=panel.querySelector('[data-record-live]');live.srcObject=stream;
  if(mode==='audio')start();
  else{
   facing=stream.getVideoTracks()[0]?.getSettings?.().facingMode||'user';setPhase('ready');
   panel.querySelector('[data-record-start]').hidden=false;panel.querySelector('[data-record-camera]').hidden=false;
   panel.querySelector('[data-record-state]').textContent='Проверьте кадр и выберите камеру';
  }
 }catch(e){if(ticket!==generation)return;release();setPhase('error');error(e.name==='NotAllowedError'?'Разрешите доступ к микрофону и камере в настройках Telegram.':e.name==='NotFoundError'?'Микрофон или камера не найдены.':e.message);}
}
async function switchCamera(){
 if(phase!=='ready')return;
 const ticket=generation,before=facing,next=facing==='user'?'environment':'user';setPhase('switching');
 const startButton=panel.querySelector('[data-record-start]'),flip=panel.querySelector('[data-record-camera]');startButton.disabled=true;flip.disabled=true;panel.querySelector('[data-record-error]').hidden=true;
 for(const track of stream.getVideoTracks()){track.stop();stream.removeTrack(track);}
 const install=async (desired,exact)=>{
  const camera=await openCamera(desired,exact);
  if(ticket!==generation){camera.getTracks().forEach(t=>t.stop());return;}
  const track=camera.getVideoTracks()[0];stream.addTrack(track);facing=track.getSettings?.().facingMode||desired;
  const live=panel.querySelector('[data-record-live]');live.srcObject=stream;await live.play().catch(()=>{});
 };
 try{await install(next,true);}
 catch{if(ticket===generation){try{await install(before,false);}catch{release();setPhase('error');}error('Другая камера недоступна. Попробуйте выбрать её ещё раз.');}}
 finally{if(ticket===generation){if(stream){setPhase('ready');startButton.disabled=false;}flip.disabled=false;flip.setAttribute('aria-label',facing==='environment'?'Включить переднюю камеру':'Включить заднюю камеру');}}
}
panel.addEventListener('click',async e=>{
 if(e.target.closest('[data-record-close]')){if(phase!=='sending')close();return;}
 if(e.target.closest('[data-record-start]'))return start();
 if(e.target.closest('[data-record-camera]'))return switchCamera();
 if(e.target.closest('[data-record-stop]'))return stop();
 const send=e.target.closest('[data-record-send]');if(!send||phase!=='preview'||!blob)return;
 setPhase('sending');send.disabled=true;panel.querySelector('[data-record-close]').disabled=true;
 panel.querySelector('[data-record-state]').textContent='Отправляем запись…';
 try{const file=new File([blob],`${mode==='audio'?'voice':'video'}-${Date.now()}.${extension(blob.type)}`,{type:blob.type});await sendFile(file);close();}
 catch(err){setPhase('preview');send.disabled=false;panel.querySelector('[data-record-close]').disabled=false;error(err.message);}
});
panel.addEventListener('cancel',e=>{e.preventDefault();if(phase!=='sending')close();});
window.addEventListener('pagehide',close);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)return;if(phase==='recording')stop();else if(['loading','ready','switching'].includes(phase))close();});
