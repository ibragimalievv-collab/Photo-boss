/* Actual check-in/out. Never simulate saved locations, photos, or shifts. */
import {api} from '/app/js/api.js';
import {esc} from '/app/js/domain.js';
import {enqueueOperation,outboxRows} from '/app/js/outbox.js';

export function phase(data) {
 if (!data?.eligible) return 'CONTROL';
 if (data.end?.status === 'FINISHED') return 'FINISHED';
 if (data.end?.status === 'PENDING_REVIEW') return 'REVIEW';
 if (data.start?.status === 'PENDING_REVIEW') return 'REVIEW_START';
 if (data.start?.status === 'STARTED') return data.end ? 'END_PHOTO' : 'STARTED';
 return data.start ? 'START_PHOTO' : 'NOT_STARTED';
}

export function getLocation(tg, browser = navigator, timeout = 16000) {
 return new Promise((resolve, reject) => {
  let done = false;
  const finish = (value, error) => { if (done) return; done=true; clearTimeout(timer); error ? reject(new Error(error)) : resolve(value); };
  const timer = setTimeout(() => finish(null, 'Не удалось получить координаты вовремя. Включите геолокацию и повторите.'), timeout);
  const accept = x => {
   if (!x) return finish(null, 'Доступ к геолокации не предоставлен. Разрешите его в настройках Telegram.');
   const lat=x.latitude, lon=x.longitude, accuracy=x.horizontal_accuracy ?? x.accuracy ?? null;
   if (!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180) return finish(null,'Телефон вернул некорректные координаты.');
   finish({latitude:lat,longitude:lon,accuracy:Number.isFinite(accuracy)?accuracy:null});
  };
  const fallback = () => {
   if (!browser.geolocation) return finish(null,'На этом устройстве геолокация недоступна. Попробуйте Telegram на телефоне.');
   browser.geolocation.getCurrentPosition(p=>accept(p.coords),e=>finish(null,e.code===1?'Разрешение на геолокацию отклонено. Включите доступ в настройках приложения.':'Координаты недоступны. Включите геолокацию и повторите.'),{enableHighAccuracy:true,timeout:14000,maximumAge:0});
  };
  const manager=tg?.LocationManager;
  if (!manager || !tg?.isVersionAtLeast?.('8.0')) return fallback();
  const request = () => {
   if (done) return;
   if (!manager.isLocationAvailable) return fallback();
   try { manager.getLocation(accept); } catch { finish(null,'Не удалось запросить геолокацию у Telegram.'); }
  };
  try { if (manager.isInited) request(); else manager.init(request); }
  catch { finish(null,'Не удалось включить геолокацию Telegram.'); }
 });
}

function cameraMessage(error) {
 if (error?.name === 'NotAllowedError' || error?.name === 'SecurityError') return 'Доступ к камере отклонён. Разрешите камеру для Telegram и повторите.';
 if (error?.name === 'NotFoundError' || error?.name === 'OverconstrainedError') return 'Подходящая камера не найдена на устройстве.';
 if (error?.name === 'NotReadableError' || error?.name === 'AbortError') return 'Камера занята другим приложением. Закройте его и повторите.';
 return 'Не удалось открыть камеру. Обновите Telegram и проверьте разрешение камеры.';
}

export async function requestCamera(mediaDevices = navigator.mediaDevices, facingMode = 'environment') {
 if (!mediaDevices?.getUserMedia) throw new Error('Живая камера недоступна в этом Telegram. Обновите приложение. Выбор фото из галереи для отметки смены отключён.');
 try {
  return await mediaDevices.getUserMedia({
   audio:false,
   video:{facingMode:{ideal:facingMode},width:{ideal:1280},height:{ideal:1280}}
  });
 } catch (error) {
  throw new Error(cameraMessage(error));
 }
}

export function cameraFrame(video) {
 if (!video || !Number.isFinite(video.videoWidth) || !Number.isFinite(video.videoHeight) || video.videoWidth < 2 || video.videoHeight < 2) {
  throw new Error('Камера ещё не готова. Подождите секунду и нажмите «Снять» ещё раз.');
 }
 const scale=Math.min(1,1280/Math.max(video.videoWidth,video.videoHeight));
 const canvas=document.createElement('canvas');
 canvas.width=Math.max(1,Math.round(video.videoWidth*scale));
 canvas.height=Math.max(1,Math.round(video.videoHeight*scale));
 const ctx=canvas.getContext('2d');
 if(!ctx)throw new Error('Не удалось подготовить снимок.');
 ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(video,0,0,canvas.width,canvas.height);
 let result=canvas.toDataURL('image/jpeg',.82);if(result.length>850000)result=canvas.toDataURL('image/jpeg',.56);
 if(result.length>850000)throw new Error('Снимок слишком большой. Переключите камеру и попробуйте ещё раз.');
 return result;
}

function install() {
 const root=document.querySelector('#app');if (!root) return;
 const link=document.createElement('link');link.rel='stylesheet';link.href='/shift/attendance.css';document.head.append(link);
 const tg=window.Telegram?.WebApp;
 const dialog=document.createElement('dialog');dialog.id='attendanceDialog';dialog.className='att-dialog';dialog.setAttribute('aria-label','Моя смена');document.body.append(dialog);
 let data=null, failure='', busy=false, photo=null, purpose='start', generation=0, loading=false;
 let point=null,pointAt=0,photoAt=null;
 const localDay=value=>new Intl.DateTimeFormat('en-CA',{timeZone:data?.timezone||'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(value);
 let cameraStream=null, cameraActive=false, cameraMode='environment';
 const clock=value=>value?new Intl.DateTimeFormat('ru-RU',{timeZone:data.timezone,hour:'2-digit',minute:'2-digit'}).format(new Date(value)):'—';
 const labels={CONTROL:'Контроль смен',NOT_STARTED:'Смена не начата',START_PHOTO:'Начало ещё не подтверждено',STARTED:'Вы на смене',END_PHOTO:'Завершение ещё не подтверждено',FINISHED:'Смена завершена',REVIEW:'Ожидает проверки руководителем',REVIEW_START:'Начало ожидает проверки'};
 const button=(label,action,kind='primary',disabled=false)=>`<button type="button" class="btn ${kind}" data-att="${action}" ${busy||disabled?'disabled':''}>${label}</button>`;
 function releaseCamera(){
  if(cameraStream){for(const track of cameraStream.getTracks?.()||[])track.stop();}
  cameraStream=null;
 }
 function stopCamera(){releaseCamera();cameraActive=false;}
 function attachCamera(){
  if(!cameraActive||!cameraStream)return;
  const video=dialog.querySelector('#attCameraVideo');if(!video)return;
  video.srcObject=cameraStream;
  const playing=video.play?.();if(playing?.catch)playing.catch(()=>{});
 }
 async function openCamera(mode=cameraMode){
  if(!data?.eligible)return;
  releaseCamera();cameraActive=true;cameraMode=mode;busy=true;failure='';draw();
  const version=generation;
  try{
   const stream=await requestCamera(navigator.mediaDevices,cameraMode);
   if(version!==generation){for(const track of stream.getTracks?.()||[])track.stop();return;}
   cameraStream=stream;
  }catch(error){if(version===generation){failure=error.message;cameraActive=false;}}
  finally{if(version===generation){busy=false;draw();}}
 }
 function card() {
  // The app clears its header when authentication or staff access is unavailable.
  if (!document.querySelector('#topbar')?.children.length) return;
  if (!['','#home','#schedule','#more','#workflow'].includes(location.hash)) return;
  if(root.querySelector('#attendanceCard'))return;
  const panel=document.createElement('section');panel.id='attendanceCard';panel.className='att-card';panel.setAttribute('aria-label','Смена и геолокация');
  const title=labels[phase(data)]||'Моя смена';
  panel.innerHTML=`<div class="att-card-copy"><div class="eyebrow">СМЕНА И ГЕОЛОКАЦИЯ</div><h2>${data?title:'Моя смена'}</h2><p>${failure?'Не удалось загрузить статус. Обновите его.':!data?'Загрузка статуса…':data.eligible?data.start?.at?`Начало ${clock(data.start.at)}${data.end?.at?` · Уход ${clock(data.end.at)}`:''}`:'Геолокация и живое фото при начале и завершении смены.':'Личные отметки фотографов и менеджеров; владелец контролирует график.'}</p></div><div class="att-card-actions">${button(!data?'Обновить':!data.eligible?'Проверить смены':phase(data)==='NOT_STARTED'?'Начать смену':phase(data)==='STARTED'?'Завершить смену':phase(data)==='FINISHED'?'Посмотреть отметки':'Продолжить отметку','open')}</div>`;
  const head=root.querySelector('.page-head');if(head)head.after(panel);else root.prepend(panel);
 }
 function updateCard(){root.querySelector('#attendanceCard')?.remove();card();}
 function message(text){const el=dialog.querySelector('#attError');if(el){el.textContent=text;el.hidden=false;}}
 async function refresh(){if(loading)return;loading=true;try{
  data=await api('/attendance');const today=localDay(new Date());
  if(data.date!==today&&data._offline)data={...data,date:today,start:null,end:null};
  for(const row of await outboxRows())if(row.kind==='attendance'&&row.date===data.date&&row.status!=='error'){
   const part=row.data.purpose==='start'?'start':'end';
   if(!['STARTED','FINISHED'].includes(data[part]?.status)&&(row.status!=='synced'||!data[part]&&data._offline))data[part]={status:row.result?.attendanceStatus||'PENDING_REVIEW',claimedAt:row.data.claimedAt};
  }
  failure='';
 }catch(e){failure=e.message;}finally{loading=false;updateCard();}}
 async function queueOffline(){
  if(!point||Date.now()-pointAt>300000||!photoAt)throw new Error('Получите свежую геолокацию и сделайте фото повторно.');
  const bytes=Uint8Array.from(atob(photo.split(',')[1]),c=>c.charCodeAt(0));
  await enqueueOperation('attendance',localDay(new Date(photoAt)),{purpose,claimedAt:photoAt,...point},new Blob([bytes],{type:'image/jpeg'}));
  data[purpose==='start'?'start':'end']={status:'PENDING_REVIEW',claimedAt:photoAt};
  photo=null;point=null;photoAt=null;
 }

 function cameraView(){
  const mirrored=cameraMode==='user'?'att-camera-mirror':'';
  dialog.innerHTML=`<div class="att-dialog-head"><div><div class="eyebrow">PHOTO BOSS · ЖИВАЯ КАМЕРА</div><h2>${purpose==='start'?'Фото начала смены':'Фото завершения смены'}</h2></div>${button('Закрыть','close','ghost')}</div><div id="attError" class="att-error" role="alert" ${failure?'':'hidden'}>${esc(failure)}</div><div class="att-camera-stage"><video id="attCameraVideo" class="att-camera-video ${mirrored}" autoplay playsinline muted></video><div class="att-camera-hint">${cameraStream?'Камера включена · снимок берётся только сейчас':'Запрашиваем доступ к камере…'}</div></div><p class="att-notice">Галерея для отметки смены отключена. Снимок можно сделать только камерой в этом окне.</p><div class="att-camera-actions">${button('Назад','camera-back','ghost')}${button(cameraMode==='user'?'Задняя камера':'Передняя камера','flip','secondary',!cameraStream)}${button('Снять','snap','primary',!cameraStream)}</div>`;
  requestAnimationFrame(attachCamera);
 }
 function draw() {
  if(!dialog.open)return;
  const p=phase(data);if(p==='END_PHOTO'||p==='STARTED'||p==='REVIEW_START')purpose='end';else if(p==='START_PHOTO'||p==='NOT_STARTED')purpose='start';
  if(cameraActive){cameraView();return;}
  const record=purpose==='start'?data?.start:data?.end;
  const fresh=!!point&&Date.now()-pointAt<=300000||!!record?.locationFresh&&!data?._offline;
  dialog.innerHTML=`<div class="att-dialog-head"><div><div class="eyebrow">PHOTO BOSS · МОЯ СМЕНА</div><h2>${data?labels[p]:'Статус смены'}</h2></div>${button('Закрыть','close','ghost')}</div><div id="attError" class="att-error" role="alert" ${failure?'':'hidden'}>${esc(failure)}</div>${!data?button('Обновить статус','reload'):p==='CONTROL'?`<p>Ваш аккаунт управляет работой команды. Личное начало смены доступно аккаунтам с ролью фотографа или менеджера записи.</p>${button('Открыть график сотрудников','schedule')}`:p==='FINISHED'?`<div class="att-done">✓ Начало: ${clock(data.start.at)}<br>✓ Завершение: ${clock(data.end.at)}<br>Геолокации и фотографии сохранены.</div>${button('Открыть краткий отчёт','report')}${button('Обновить','reload','ghost')}`:p==='REVIEW'?`<p>Фото, координаты и время отправлены на проверку. Синхронизация ещё не подтверждает присутствие. До решения руководителя штраф не начисляется.</p>${button('Проверить синхронизацию','queue','secondary')}${button('Обновить','reload','ghost')}`:`${p==='REVIEW_START'?'<p class="notice">Начало сохранено для проверки. Можно сохранить окончание смены; руководитель проверит обе отметки.</p>':''}<p>${purpose==='start'?'Чтобы начать смену, отправьте геолокацию и свежее фото в полный рост.':'Чтобы завершить смену, отправьте геолокацию и фото рабочего места.'}</p><div class="att-steps"><div><span class="att-step">1</span><div><strong>Геолокация</strong><p>${fresh?'✓ Координаты сохранены.':record?.locationSaved?'Координаты устарели. Получите их повторно.':'Местоположение запрашивается только по нажатию.'}</p>${button(fresh?'Получить заново':'Проверить геолокацию','location','secondary')} ${button('Настройки доступа','settings','ghost')}</div></div><div><span class="att-step">2</span><div><strong>${purpose==='start'?'Фото в полный рост':'Фото рабочего места'}</strong><p>Фото делается только сейчас через камеру. Выбор из галереи отключён.</p>${button(photo?'Переснять':'Сделать фото','camera','secondary',!fresh)}${photo?`<img class="att-preview" alt="Сделанная фотография" src="${photo}">`:''}</div></div></div><p class="att-notice">${esc(data.geoNotice)} Фото берётся из живого видеопотока камеры непосредственно перед подтверждением.</p>${purpose==='start'?`<p class="att-notice">Правило действующего бота: начало после ${esc(data.startTime)} — штраф ${data.lateFine} ₽. Подтверждение создаёт настоящую отметку, не учебную.</p>`:''}<button type="button" class="btn primary full-width" data-att="confirm" ${!photo||!fresh||busy?'disabled':''}>${busy?'Сохраняем…':purpose==='start'?'Подтвердить начало смены':'Подтвердить завершение смены'}</button><p class="att-caption">При отсутствии связи отметка сохраняется на устройстве, затем отправляется руководителю для проверки. Время телефона само по себе не подтверждает присутствие.</p>`}`;
 }
 async function open(){generation++;stopCamera();photo=null;point=null;photoAt=null;if(!dialog.open)dialog.showModal();draw();await refresh();draw();}
 function close(){if(busy)return;generation++;stopCamera();photo=null;dialog.close();}
 document.addEventListener('click',async e=>{
  const target=e.target.closest('button');if(!target)return;
  if(target.dataset.action==='shift'){e.preventDefault();e.stopImmediatePropagation();await open();return;}
  if(!target.dataset.att)return;e.preventDefault();e.stopImmediatePropagation();
  const act=target.dataset.att;
  if(act==='close')return close();
  if(act==='open')return open();
  if(act==='reload'){await refresh();draw();return;}
  if(act==='schedule'){close();location.hash='schedule';return;}
  if(act==='report'){close();location.hash='workday';return;}
  if(act==='queue'){close();location.hash='workflow';return;}
  if(act==='settings'){try{if(tg?.LocationManager?.openSettings)tg.LocationManager.openSettings();else message('Разрешите местоположение и камеру в настройках разрешений Telegram.');}catch{message('Откройте настройки разрешений Telegram на телефоне.');}return;}
  if(act==='camera'){
   if(busy||!data?.eligible)return;
   cameraMode=purpose==='start'?'user':'environment';
   await openCamera(cameraMode);return;
  }
  if(act==='camera-back'){stopCamera();draw();return;}
  if(act==='flip'){
   if(busy||!cameraActive)return;
   cameraMode=cameraMode==='user'?'environment':'user';
   await openCamera(cameraMode);return;
  }
  if(act==='snap'){
   if(busy||!cameraStream)return;
   try{photo=cameraFrame(dialog.querySelector('#attCameraVideo'));photoAt=new Date().toISOString();failure='';stopCamera();draw();}
   catch(error){failure=error.message;draw();}
   return;
  }
  if(busy||!data?.eligible)return;
  const version=generation;let openReport=false;busy=true;draw();
  try{
   if(act==='location'){
    const captured=await getLocation(tg);if(version!==generation)return;
    const claimedDate=localDay(new Date());
    if(navigator.onLine&&data.start?.status!=='PENDING_REVIEW'){
     try{data=await api('/attendance/location',{method:'POST',body:{purpose,date:claimedDate,...captured}});}
     catch(e){if(e.status&&e.status<500)throw e;}
    }
    point=captured;pointAt=Date.now();data.date=claimedDate;photo=null;photoAt=null;
   }else if(act==='confirm'){
    if(!photo)throw new Error('Сначала сделайте фотографию камерой.');
    if(!navigator.onLine||data.start?.status==='PENDING_REVIEW')await queueOffline();
    else try{data=await api('/attendance/photo',{method:'POST',body:{purpose,date:data.date,image:photo}});photo=null;point=null;}
    catch(e){if(e.status&&e.status<500)throw e;await queueOffline();}
    openReport=purpose==='end';
   }
   failure='';
  }catch(error){failure=error.message;}
  finally{busy=false;updateCard();draw();if(openReport){close();location.hash='workday';}}
 },true);
 dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();else{generation++;stopCamera();photo=null;}});
 new MutationObserver(card).observe(root,{childList:true,subtree:false});
 window.addEventListener('hashchange',()=>{if(!busy&&dialog.open)close();card();});
 document.addEventListener('visibilitychange',()=>{
  if(document.hidden){if(cameraActive){stopCamera();failure='Камера была выключена, потому что приложение свернули.';}}
  else if(!busy){refresh().then(draw);}
 });
 refresh();card();
}
if(typeof document!=='undefined')install();
