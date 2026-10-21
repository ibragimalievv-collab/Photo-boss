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
 let data=null, failure='', busy=false, photo=null, purpose='start', retakePurpose=null, generation=0, loading=false, refreshAgain=false;
 let point=null,pointAt=0,photoAt=null,pointSaved=false;
 const localDay=value=>new Intl.DateTimeFormat('en-CA',{timeZone:data?.timezone||'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(value);
 let cameraStream=null, cameraActive=false, cameraMode='environment';
 const clock=value=>value?new Intl.DateTimeFormat('ru-RU',{timeZone:data.timezone,hour:'2-digit',minute:'2-digit'}).format(new Date(value)):'—';
 const gpsRequired=()=>data?.gpsRequired!==false;
 const geofenceRequired=()=>gpsRequired()&&data?.geofenceRequired!==false;
 const freshPhoto=()=>!!photo&&!!photoAt&&Date.now()-new Date(photoAt).getTime()>=0&&Date.now()-new Date(photoAt).getTime()<=300000;
 const storedPending=record=>record?.status==='PENDING_REVIEW'&&!!record.photoSaved&&(!gpsRequired()||!!record.locationSaved)&&!!record.claimedAt&&!record._localOnly;
 const startAvailable=()=>data?.start?.status==='STARTED'||storedPending(data?.start);
 const canRetake=part=>(!gpsRequired()||!geofenceRequired())&&data?.[part]?.status==='PENDING_REVIEW'&&!data[part]._localOnly&&data[part].evidenceSource!=='offline'&&(part==='start'||data.start?.status==='STARTED');
 const retakeLabel=part=>gpsRequired()?(part==='start'?'Повторно подтвердить начало':'Повторно подтвердить завершение'):(part==='start'?'Подтвердить начало по свежему фото':'Подтвердить завершение по свежему фото');
 const title=()=>phase(data)==='REVIEW_START'&&!startAvailable()?'Начало ожидает синхронизации':phase(data)==='REVIEW'&&data.end?._localOnly?'Завершение ожидает синхронизации':labels[phase(data)]||'Моя смена';
 const labels={CONTROL:'Контроль смен',NOT_STARTED:'Смена не начата',START_PHOTO:'Начало ещё не подтверждено',STARTED:'Вы на смене',END_PHOTO:'Завершение ещё не подтверждено',FINISHED:'Смена завершена',REVIEW:'Ожидает проверки руководителем',REVIEW_START:'Начало сохранено · требуется проверка'};
 const button=(label,action,kind='primary',disabled=false)=>`<button type="button" class="btn ${kind}" data-att="${action}" ${disabled||busy&&!(cameraActive&&['close','camera-back'].includes(action))?'disabled':''}>${label}</button>`;
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
  const panel=document.createElement('section');panel.id='attendanceCard';panel.className='att-card';panel.setAttribute('aria-label',gpsRequired()?'Смена и геолокация':'Моя смена');
  panel.innerHTML=`<div class="att-card-copy"><div class="eyebrow">${gpsRequired()?'СМЕНА И ГЕОЛОКАЦИЯ':'МОЯ СМЕНА'}</div><h2>${data?title():'Моя смена'}</h2><p>${failure?'Не удалось загрузить статус. Обновите его.':!data?'Загрузка статуса…':data.eligible?data.start?.at?`Начало ${clock(data.start.at)}${data.end?.at?` · Уход ${clock(data.end.at)}`:''}`:gpsRequired()?'Геолокация и живое фото при начале и завершении смены.':'Живое фото начала и завершения. Геолокация временно отключена.':'Личные отметки фотографов и менеджеров; владелец контролирует график.'}</p></div><div class="att-card-actions">${button(!data?'Обновить':!data.eligible?'Проверить смены':phase(data)==='NOT_STARTED'?'Начать смену':phase(data)==='STARTED'?'Завершить смену':phase(data)==='FINISHED'?'Посмотреть отметки':'Продолжить отметку','open')}</div>`;
  const head=root.querySelector('.page-head');if(head)head.after(panel);else root.prepend(panel);
 }
 function updateCard(){root.querySelector('#attendanceCard')?.remove();card();}
 function message(text){const el=dialog.querySelector('#attError');if(el){el.textContent=text;el.hidden=false;}}
 async function refresh(){if(loading){refreshAgain=true;return;}loading=true;const version=generation;try{
  const latest=await api('/attendance');if(version!==generation)return;data=latest;const today=localDay(new Date());
  if(data.date!==today&&data._offline)data={...data,date:today,start:null,end:null};
  const rows=await outboxRows();if(version!==generation)return;
  for(const row of rows)if(row.kind==='attendance'&&row.date===data.date&&row.status!=='error'){
   const part=row.data.purpose==='start'?'start':'end';
   if(!['STARTED','FINISHED'].includes(data[part]?.status)&&!storedPending(data[part])&&(row.status!=='synced'||!data[part]&&data._offline))data[part]={status:row.result?.attendanceStatus||'PENDING_REVIEW',claimedAt:row.data.claimedAt,_localOnly:row.status!=='synced',photoSaved:row.status==='synced',locationSaved:row.status==='synced'&&row.data.latitude!=null,evidenceSource:'offline'};
  }
  failure='';
 }catch(e){failure=e.message;}finally{loading=false;updateCard();draw();if(refreshAgain){refreshAgain=false;await refresh();}}}
 async function queueOffline(){
  if(!freshPhoto())throw new Error('Сделайте свежее фото камерой повторно.');
  if(gpsRequired()&&(!point||Date.now()-pointAt>300000))throw new Error('Получите свежую геолокацию и сделайте фото повторно.');
  const version=generation;
  const bytes=Uint8Array.from(atob(photo.split(',')[1]),c=>c.charCodeAt(0));
  await enqueueOperation('attendance',localDay(new Date(photoAt)),{purpose,claimedAt:photoAt,...(gpsRequired()?point:{latitude:null,longitude:null,accuracy:null})},new Blob([bytes],{type:'image/jpeg'}));
  if(version!==generation)return;
  data[purpose==='start'?'start':'end']={status:'PENDING_REVIEW',claimedAt:photoAt,_localOnly:true,evidenceSource:'offline'};
  retakePurpose=null;photo=null;point=null;photoAt=null;pointSaved=false;
 }

 function cameraView(){
  const mirrored=cameraMode==='user'?'att-camera-mirror':'';
  dialog.innerHTML=`<div class="att-dialog-head"><div><div class="eyebrow">PHOTO BOSS · ЖИВАЯ КАМЕРА</div><h2>${purpose==='start'?'Фото начала смены':'Фото завершения смены'}</h2></div>${button('Закрыть','close','ghost')}</div><div id="attError" class="att-error" role="alert" ${failure?'':'hidden'}>${esc(failure)}</div><div class="att-camera-stage"><video id="attCameraVideo" class="att-camera-video ${mirrored}" autoplay playsinline muted></video><div class="att-camera-hint">${cameraStream?'Камера включена · снимок берётся только сейчас':'Запрашиваем доступ к камере…'}</div></div><p class="att-notice">Галерея для отметки смены отключена. Снимок можно сделать только камерой в этом окне.</p><div class="att-camera-actions">${button('Назад','camera-back','ghost')}${button(cameraMode==='user'?'Задняя камера':'Передняя камера','flip','secondary',!cameraStream)}${button('Снять','snap','primary',!cameraStream)}</div>`;
  requestAnimationFrame(attachCamera);
 }
 function draw() {
  if(!dialog.open)return;
  const currentPhase=phase(data);
  if(retakePurpose&&!canRetake(retakePurpose))retakePurpose=null;
  const p=retakePurpose==='start'?'START_PHOTO':retakePurpose==='end'?'END_PHOTO':currentPhase;
  if(p==='END_PHOTO'||p==='STARTED'||p==='REVIEW_START')purpose='end';else if(p==='START_PHOTO'||p==='NOT_STARTED')purpose='start';
  if(cameraActive){cameraView();return;}
  const record=purpose==='start'?data?.start:data?.end;
  const geo=geofenceRequired()?(record?.geofence||(p==='REVIEW_START'?data?.start?.geofence:null)):null;
  const geoText=geo?`${geo.hotelName||'Отель не определён'}${Number.isFinite(geo.distanceM)?` · расстояние ${Math.round(geo.distanceM)} м, радиус ${geo.radiusM} м`:''}${geo.accuracyM!=null?` · погрешность ${Math.round(geo.accuracyM)} м`:''}. ${geo.notice}`:'';
  const fresh=!!point&&Date.now()-pointAt<=300000||!retakePurpose&&!!record?.locationFresh&&!data?._offline&&Date.now()-new Date(record.locationReceivedAt).getTime()<=300000;
  const ready=!gpsRequired()||fresh;
  const evidence=gpsRequired()?'фото и координат':'фото';
  const policyNotice=data?.eligible?(!gpsRequired()?'<p class="att-notice">Геолокация временно отключена. Координаты не запрашиваются и местоположение не проверяется. Начало и завершение подтверждаются по свежему фото с камеры.</p>':!geofenceRequired()?'<p class="att-notice">Геолокация обязательна: сохраните свежие координаты и фото. Проверка расстояния до отеля временно отключена; метки отелей будут настроены позже. Сохранение GPS не подтверждает нахождение рядом с отелем.</p>':''):'';
  const retakeNotice=retakePurpose?`<p class="notice">${gpsRequired()?'Заново получите геолокацию, затем сделайте новое фото':'Сделайте новое фото'} ${purpose==='start'?'в полный рост для подтверждения начала':'рабочего места для подтверждения завершения'} смены. Старая отметка останется в истории проверки.</p>${button('Назад к отметкам','retake-cancel','ghost')}`:'';
  const pendingStart=p==='REVIEW_START'?(startAvailable()?`<p class="notice">Начало смены записано на сервере. ${data.start.evidenceSource==='offline'?'Офлайн-отметку проверяет руководитель.':geofenceRequired()?esc(data.start?.geofence?.notice||'Этой отметке нужна ручная проверка координат и фото.'):'Эта отметка ожидает проверки по прежнему правилу.'} Работа и обед доступны: повторно начинать смену не нужно. Руководитель может проверить сохранённую отметку в контроле смен. Ниже — оформление завершения смены.</p>`:`<p class="notice">${data.start?._localOnly?'Начало смены сохранено на этом устройстве.':'Нет полного подтверждения начала смены.'} Отправка ${evidence} на сервер ещё не подтверждена. Подключитесь к сети и проверьте синхронизацию; обед станет доступен после принятия отметки сервером. При отсутствии связи можно также сохранить завершение смены на устройстве.</p>${button('Проверить синхронизацию','queue','secondary')}${button('Обновить статус','reload','ghost')}`)+(canRetake('start')?button(retakeLabel('start'),'retake-start','primary'):''):'';
  const locationStep=gpsRequired()?`<div><span class="att-step">1</span><div><strong>Геолокация</strong><p>${fresh?(point&&!pointSaved?'✓ Координаты получены на устройстве. Сервер ещё не подтвердил сохранение.':'✓ Координаты сохранены на сервере.'):record?.locationSaved?'Координаты устарели. Получите их повторно.':'Местоположение запрашивается только по нажатию.'}</p>${button(fresh?'Получить заново':'Проверить геолокацию','location','secondary')} ${button('Настройки доступа','settings','ghost')}</div></div>`:'';
  const captureView=`${retakeNotice}${['STARTED','REVIEW_START'].includes(p)&&startAvailable()?button('Обед / вернулся с обеда','lunch','secondary'):''}${pendingStart}<p>${purpose==='start'?`Чтобы начать смену, отправьте ${gpsRequired()?'геолокацию и ':''}свежее фото в полный рост.`:`Чтобы завершить смену, отправьте ${gpsRequired()?'геолокацию и ':''}фото рабочего места.`}</p><div class="att-steps">${locationStep}<div><span class="att-step">${gpsRequired()?'2':'1'}</span><div><strong>${purpose==='start'?'Фото в полный рост':'Фото рабочего места'}</strong><p>Фото делается только сейчас через камеру. Выбор из галереи отключён.</p>${button(photo?'Переснять':'Сделать фото','camera','secondary',!ready)}${photo?`<img class="att-preview" alt="Сделанная фотография" src="${photo}">`:''}</div></div></div><p class="att-notice">${esc(geoText)} ${gpsRequired()?esc(data?.geoNotice):''} Фото берётся из живого видеопотока камеры непосредственно перед подтверждением.</p>${geofenceRequired()&&data?.reviewRequired?'<p class="att-notice">Внутри настроенной зоны отеля смена подтверждается автоматически после сохранения фото. Если координаты неточные, вне зоны или зона не настроена — проверяет руководитель. Штраф по времени рассчитывается автоматически.</p>':''}<button type="button" class="btn primary full-width" data-att="confirm" ${!freshPhoto()||!ready||busy?'disabled':''}>${busy?'Сохраняем…':geofenceRequired()&&data?.reviewRequired?(geo?.state==='inside'?'Подтвердить смену':'Сохранить для проверки'):purpose==='start'?'Подтвердить начало смены':'Подтвердить завершение смены'}</button><p class="att-caption">При отсутствии связи отметка сохраняется на устройстве, затем отправляется руководителю для проверки. Время телефона само по себе не подтверждает присутствие.</p>`;
  dialog.innerHTML=`<div class="att-dialog-head"><div><div class="eyebrow">PHOTO BOSS · МОЯ СМЕНА</div><h2>${data?retakePurpose?purpose==='start'?'Новое фото начала смены':'Новое фото завершения смены':title():'Статус смены'}</h2></div>${button('Закрыть','close','ghost')}</div><div id="attError" class="att-error" role="alert" ${failure?'':'hidden'}>${esc(failure)}</div>${policyNotice}${!data?button('Обновить статус','reload'):p==='CONTROL'?`<p>Ваш аккаунт управляет работой команды. Личное начало смены доступно аккаунтам с ролью фотографа или менеджера записи.</p>${button('Открыть график сотрудников','schedule')}`:p==='FINISHED'?`<div class="att-done">✓ Начало: ${clock(data.start.at)}<br>✓ Завершение: ${clock(data.end.at)}<br>${gpsRequired()?'Геолокации и фотографии':'Фотографии и время'} сохранены.</div>${button('Открыть краткий отчёт','report')}${button('Обновить','reload','ghost')}`:p==='REVIEW'?`<p>${data.end?._localOnly?`Завершение смены сохранено на этом устройстве. Отправка ${evidence} и времени на сервер ещё не подтверждена. Подключитесь к сети и проверьте синхронизацию.`:'Данные отметки отправлены на проверку. Синхронизация ещё не подтверждает присутствие. Штрафы по времени рассчитываются автоматически; уважительную причину можно учесть при проверке.'}</p>${canRetake('start')?`<p class="notice">Сначала повторно подтвердите начало. Затем можно подтвердить завершение.</p>${button(retakeLabel('start'),'retake-start','primary')}`:''}${canRetake('end')?button(retakeLabel('end'),'retake-end','primary'):''}${button('Проверить синхронизацию','queue','secondary')}${button('Обновить','reload','ghost')}`:captureView}`;
 }
 async function open(){generation++;stopCamera();retakePurpose=null;photo=null;point=null;pointSaved=false;photoAt=null;if(!dialog.open)dialog.showModal();draw();await refresh();draw();}
 function close(){if(busy&&!cameraActive)return;generation++;stopCamera();busy=false;photo=null;dialog.close();}
 document.addEventListener('click',async e=>{
  const target=e.target.closest('button');if(!target)return;
  if(target.dataset.action==='shift'){e.preventDefault();e.stopImmediatePropagation();await open();return;}
  if(!target.dataset.att)return;e.preventDefault();e.stopImmediatePropagation();
  const act=target.dataset.att;
  if(act==='close')return close();
  if(act==='open')return open();
  if(act==='reload'){await refresh();draw();return;}
  if(act==='schedule'){close();location.hash='schedule';return;}
  if(act==='report'||act==='lunch'){close();location.hash='workday';return;}
  if(act==='queue'){close();location.hash='workflow';return;}
  if(act==='retake-cancel'){retakePurpose=null;photo=null;photoAt=null;draw();return;}
  if(act==='retake-start'||act==='retake-end'){
   const part=act==='retake-start'?'start':'end';if(busy||!canRetake(part))return;
   retakePurpose=part;photo=null;photoAt=null;point=null;pointSaved=false;failure='';draw();return;
  }
  if(act==='settings'){try{if(tg?.LocationManager?.openSettings)tg.LocationManager.openSettings();else message('Разрешите местоположение и камеру в настройках разрешений Telegram.');}catch{message('Откройте настройки разрешений Telegram на телефоне.');}return;}
  if(act==='camera'){
   if(busy||!data?.eligible)return;
   cameraMode=purpose==='start'?'user':'environment';
   await openCamera(cameraMode);return;
  }
  if(act==='camera-back'){generation++;stopCamera();busy=false;draw();return;}
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
    if(!gpsRequired())return;
    const captured=await getLocation(tg);if(version!==generation)return;
    const claimedDate=localDay(new Date());
    pointSaved=false;
    if(navigator.onLine){
     try{const saved=await api('/attendance/location',{method:'POST',body:{purpose,date:claimedDate,...captured}});if(version!==generation)return;data=saved;pointSaved=true;}
     catch(e){if(e.status&&e.status<500)throw e;}
    }
    point=captured;pointAt=Date.now();data.date=claimedDate;photo=null;photoAt=null;
   }else if(act==='confirm'){
    if(!freshPhoto())throw new Error('Сначала сделайте свежее фото камерой.');
    if(!navigator.onLine)await queueOffline();
    else try{
     // A device can report online while the earlier GPS request never reached
     // the server. Reconcile that step before sending a photo-only request.
     if(gpsRequired()&&point&&!pointSaved){
      if(Date.now()-pointAt>300000)throw new Error('Геолокация устарела. Получите её повторно.');
      const saved=await api('/attendance/location',{method:'POST',body:{purpose,date:data.date,...point}});
      if(version!==generation)return;data=saved;pointSaved=true;
     }
     const saved=await api('/attendance/photo',{method:'POST',body:{purpose,date:gpsRequired()?data.date:localDay(new Date(photoAt)),image:photo}});if(version!==generation)return;data=saved;retakePurpose=null;photo=null;point=null;pointSaved=false;
    }
    catch(e){if(e.status&&e.status<500)throw e;await queueOffline();}
    if(version!==generation)return;
    openReport=purpose==='end';
   }
   failure='';
  }catch(error){if(version===generation)failure=error.message;}
  finally{if(version===generation){busy=false;updateCard();draw();if(openReport){close();location.hash='workday';}}}
 },true);
 dialog.addEventListener('cancel',e=>{if(busy&&!cameraActive)e.preventDefault();else{generation++;stopCamera();busy=false;photo=null;}});
 new MutationObserver(card).observe(root,{childList:true,subtree:false});
 window.addEventListener('hashchange',()=>{if(!busy&&dialog.open)close();card();});
 window.addEventListener('pb-operations-reset',()=>{
  const required=gpsRequired();
  generation++;stopCamera();retakePurpose=null;photo=null;point=null;pointAt=0;pointSaved=false;photoAt=null;data=null;busy=false;
  failure=required?'Рабочие данные обнулены. Получите геолокацию и сделайте новое фото.':'Рабочие данные обнулены. Сделайте новое фото камерой.';
  if(dialog.open)dialog.close();updateCard();
  // A pre-reset status request may still finish; its generation check above
  // discards it. The next open/visibility refresh fetches the cleared ledger.
 });
 document.addEventListener('visibilitychange',()=>{
  if(document.hidden){if(cameraActive){generation++;stopCamera();busy=false;failure='Камера была выключена, потому что приложение свернули.';}}
  else if(!busy){refresh().then(draw);}
 });
 refresh();card();
}
if(typeof document!=='undefined')install();
