import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox,enqueueOperation,enqueueFilesOneByOne} from './outbox.js';
let data=null,queue=[],selected=null,folderBusy=false;
const labels={local:'В очереди на этом устройстве — не отправлено',syncing:'Передаётся на сервер',synced:'Сервер принял',error:'Ошибка отправки'};
const kinds={booking:'Бронь',booking_receipt:'Чек брони',sale_start:'Черновик продажи',sale_receipt:'Чек продажи',sale_counts:'Количество кадров',sale_selected:'Выбранный кадр',sale_complete:'Оформление продажи',shoot_photo:'Кадр полной съёмки',shoot_complete:'Завершение загрузки',checklist:'Контрольный пункт',shift_report:'Замечание',attendance:'Отметка смены'};
export async function loadWorkflow(){data=await api('/workflow');queue=await outboxRows();if(selected&&!data.bookings.some(b=>b.id===selected))selected=null;scheduleStorageRefresh();}
const field=(label,input)=>`<label class="field">${label}${input}</label>`;
const input=(name,attrs='')=>`<input class="input" name="${name}" ${attrs}>`;
const file=(name,multiple=false,required=true)=>input(name,`type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" ${multiple?'multiple':''} ${required?'required':''}`);
const options=rows=>rows.map(b=>`<option value="${b.id}">${esc(b.name||`№${b.id} · ${b.date} ${b.time} · комната ${b.room}`)}</option>`).join('');

const editStatus={PENDING:'В очереди',RUNNING:'Обрабатывается',READY:'Копия сохранена',FAILED:'Ошибка',CANCELLED:'Отменена'};
function editView(b){if(!b.canUpload)return '';return `<details><summary>Обработка · оригиналы сохраняются отдельно</summary><p>AI подбирает только свет и цвет. Ручные настройки доступны независимо от AI. Обработанные копии не заменяют оригиналы.</p>${(b.files||[]).filter(f=>f.status==='STORED').map(f=>`<details><summary>Кадр №${f.id}</summary><form class="form-grid photo-edit" data-shooting="${b.shootingId}" data-photo="${f.id}">${[['exposure','Экспозиция (EV)',0,-1.5,1.5],['contrast','Контраст',1,.8,1.2],['color','Насыщенность',1,.8,1.2],['red','Баланс белого: красный',1,.85,1.15],['blue','Баланс белого: синий',1,.85,1.15]].map(([key,label,val,min,max])=>field(label,input(key,`type="number" value="${val}" min="${min}" max="${max}" step="0.05" required`))).join('')}<button class="btn primary">Создать обработанную копию</button></form><button class="btn ghost" data-workflow="edit-ai" data-shooting="${b.shootingId}" data-photo="${f.id}">Подобрать свет и цвет с AI</button>${(b.edits||[]).filter(e=>e.photo===f.id).map(e=>`<article><p>Версия №${e.id}: ${esc(editStatus[e.status])} ${esc(e.error||'')}</p>${e.status==='READY'?`<button class="btn ghost" data-workflow="compare" data-photo="${f.id}" data-edit="${e.id}">Сравнить с оригиналом</button>`:''}${e.status==='FAILED'?`<button class="btn ghost" data-workflow="edit-retry" data-shooting="${b.shootingId}" data-photo="${f.id}" data-edit="${e.id}">Повторить обработку</button>`:''}${e.status!=='CANCELLED'?`<button class="btn ghost" data-workflow="edit-cancel" data-shooting="${b.shootingId}" data-photo="${f.id}" data-edit="${e.id}">Отменить результат</button>`:''}<div data-compare="${e.id}"></div></article>`).join('')}</details>`).join('')}</details>`;}
let compareURLs=[];
function clearComparisons(){for(const url of compareURLs)URL.revokeObjectURL(url);compareURLs=[];}
window.addEventListener('hashchange',clearComparisons);window.addEventListener('pagehide',clearComparisons);

function queueView(){
 const inProgress=queue.filter(r=>r.status==='local'||r.status==='syncing').length;
 const errors=queue.filter(r=>r.status==='error').length;
 const stored=queue.filter(r=>r.status==='synced').length;
 const visible=[...queue.filter(r=>r.status!=='synced').slice(0,50),...queue.filter(r=>r.status==='synced').slice(-20)];
 const summary=`<p class="small">Ожидают отправки или отправляются: ${inProgress} · Ошибки: ${errors} · Принято сервером: ${stored}. Показаны первые 50 незавершённых и последние 20 завершённых операций.</p>`;
 return summary+(visible.map(r=>`<article class="panel"><strong>${r.kind==='shoot_photo'&&r.status==='synced'?'Сервер принял · проверьте Яндекс.Диск ниже':labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.photoId?` · кадр №${r.result.photoId}`:''}${r.filename?` · ${esc(r.filename)}`:''}${r.data?.shooting?.id?` · съёмка №${r.data.shooting.id}`:''}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.status==='error'||(r.status==='local'&&r.error)?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">Повторить</button>`:''}</article>`).join('')||'<p>Операций в очереди нет.</p>');
}
function connectionNote(){
 const offline=data?._offline?(`${navigator.onLine?'Сеть доступна, но справочники взяты из локальной копии.':'Нет сети. Справочники сохранены.'} ${new Date(data._cachedAt).toLocaleString('ru-RU')}. `):'';
 return offline+'Фотографии сначала попадают в очередь именно этого браузера на этом устройстве. Пока сервер не принял их, не очищайте данные браузера. После передачи проверяйте отдельный статус «Сохранено на Яндекс.Диске».';
}
function storageView(){return `<section id="workflowFiles" class="panel section"><h2>Файлы на Яндекс.Диске</h2><button class="btn ghost" data-workflow="refresh">Обновить состояние файлов</button>${data.bookings.filter(b=>b.shootingId).map(b=>`<details><summary>Съёмка №${b.shootingId} · ${esc(b.date)} · комната ${esc(b.room)} · сохранено ${(b.files||[]).filter(f=>f.status==='STORED').length} из ${(b.files||[]).length}</summary>${(b.files||[]).map(f=>`<p>Кадр №${f.id}: ${esc(({STORED:'Сохранено на Диске',PENDING:'Ожидает сохранения',UPLOADING:'Сохраняется',FAILED:'Ошибка сохранения'})[f.status])} ${esc(f.error||'')}</p>`).join('')}${editView(b)}${b.canUpload&&(b.files||[]).some(f=>f.status==='FAILED')?`<button class="btn ghost" data-workflow="retry-storage" data-shooting="${b.shootingId}">Повторить только неудачные загрузки</button>`:''}</details>`).join('')||'<p>Съёмок пока нет.</p>'}</section>`;}
export function workflowView(){
 const ready=data.bookings.filter(b=>b.status==='READY_FOR_SALE'),b=data.bookings.find(b=>b.id===selected),d=b?.draft;
 const upload=data.bookings.filter(b=>b.status==='READY_FOR_SALE'&&!b.fullUploaded&&b.canUpload);
 return `<h1>Рабочие операции</h1><div id="workflowNotice" class="notice">${esc(connectionNote())}</div>
 ${data.canBook?`<details class="panel section"><summary>Новая бронь</summary><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон гостя',input('client_phone','type="tel" required minlength="5" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Заявленная предоплата, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="0" required'))}${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа</h2><p>Выбранные кадры обязательны; оплата подтверждается владельцем.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>№${x.id} · ${esc(x.date)} · комната ${esc(x.room)}</option>`).join('')}</select>${b?(d&&!d.mine?'<p>Продажу оформляет другой сотрудник.</p>':`<form id="workflowSale" class="form-grid section"><p>Цена кадра по текущему пакету: ${esc(b.price)} ₽. Итог рассчитает сервер.</p>${!d||d.status==='AWAITING_RECEIPT'?field('Чек продажи',file('receipt')):''}${field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${d?.total||''}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}${field('Продано кадров',input('sold',`type="number" min="1" max="10000" required value="${d?.sold||''}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}<p>Уже сохранено выбранных кадров: ${d?.selected||0}. Прикрепите оставшиеся.</p>${field('Выбранные фотографии',file('selected',true,false))}<button class="btn primary">Сохранить продажу и фотографии</button></form>`):''}</section>
 <details class="panel section"><summary>Загрузить полную съёмку</summary>${upload.length?`<form id="workflowShoot" class="form-grid">${field('Съёмка',`<select class="input" name="booking">${options(upload)}</select>`)}${field('Фотографии',file('photos',true,false))}<label><input type="checkbox" name="finish"> Все кадры добавлены — завершить загрузку</label><p>Можно добавлять частями. Завершайте загрузку после того, как все кадры сохранены на Яндекс.Диске и в очереди нет ошибок. Ставка фотографа фиксируется сервером после подтверждения полной загрузки.</p><button class="btn primary">Сохранить фотографии</button></form><label class="field section"><span>Выбрать папку — отправка сразу, по одному файлу</span><input id="workflowShootFolder" class="input" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" webkitdirectory multiple></label><p id="workflowFolderProgress" class="notice" role="status">После выбора папки не закрывайте вкладку до конца передачи. Проверяйте Яндекс.Диск отдельно.</p>`:'<p>Нет доступных съёмок для полной загрузки.</p>'}</details>
 ${storageView()}
 <section class="section"><h2>Синхронизация</h2><button class="btn ghost" data-workflow="sync">Синхронизировать сейчас</button><div id="workflowSyncNote" class="notice" role="status">Отправка запускается автоматически при наличии сети. Нажмите кнопку для ручной проверки.</div><div id="workflowQueue">${queueView()}</div></section>`;
}
export function normalizeImageFile(file){
 const allowed=['image/jpeg','image/png','image/webp'];
 const inferred=({jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',webp:'image/webp'})[file.name?.split('.').pop().toLowerCase()];
 const type=allowed.includes(file.type)?file.type:!file.type?inferred:null;
 if(!type)return null;
 return file.type===type?file:new File([file],file.name,{type,lastModified:file.lastModified});
}
function imageFiles(form,name,maximum=20*1024*1024){const list=[...form.elements[name].files].map(normalizeImageFile);for(const f of list)if(!f||!f.size||f.size>maximum)throw new Error(`Изображения JPEG, PNG или WebP, не больше ${maximum/1024/1024} МБ.`);return list;}
export async function workflowFolderChange(input){
 if(folderBusy)throw new Error('Другая папка уже загружается.');
 const form=document.querySelector('#workflowShoot');
 const booking=data?.bookings.find(b=>b.id===Number(form?.elements.booking.value));
 if(!booking?.canUpload||!booking.shootingId)throw new Error('Выберите доступную съёмку.');
 const chosen=[...input.files];
 const files=chosen.map(normalizeImageFile).filter(f=>f&&f.size>0&&f.size<=20*1024*1024);
 if(!files.length)throw new Error('В папке нет JPEG, PNG или WebP до 20 МБ.');
 const note=document.querySelector('#workflowFolderProgress'),tell=value=>{if(note?.isConnected)note.textContent=value;};
 const skipped=chosen.length-files.length;
 folderBusy=true;input.disabled=true;
 try{
  tell(`Папка: ${files.length} фото. ${skipped?`Пропущено неподходящих: ${skipped}. `:''}Отправка началась…`);
  await enqueueFilesOneByOne(files,file=>({kind:'shoot_photo',date:todayKey(),data:{shooting:{id:booking.shootingId}}}),progress=>{
   tell(`Сервер принял ${progress.done} из ${progress.total}. ${progress.state==='sending'?'Отправляем':'Передан'}: ${progress.name}. Статус Диска смотрите ниже.`);
  });
  tell(`Сервер принял все ${files.length} кадров. Обновите «Файлы на Яндекс.Диске»: только статус «Сохранено на Диске» подтверждает выгрузку.`);
 }finally{folderBusy=false;input.disabled=false;input.value='';}
}
export async function workflowSubmit(form){
 const f=new FormData(form),date=todayKey(),items=[],add=(kind,data,blob=null)=>{const row={key:crypto.randomUUID(),kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.classList.contains('photo-edit')){add('photo_edit',{shooting:Number(form.dataset.shooting),photo:Number(form.dataset.photo),action:'MANUAL',edit:null,parameters:Object.fromEntries(['exposure','contrast','color','red','blue'].map(k=>[k,Number(f.get(k))]))});
 }else if(form.id==='workflowBooking'){
  const receipt=imageFiles(form,'receipt',8*1024*1024)[0],deposit=f.get('deposit');if(!String(f.get('client_phone')||'').trim())throw new Error('Укажите телефон гостя.');if(Number(deposit)>0&&!receipt)throw new Error('Прикрепите чек заявленной предоплаты.');
  const booking=add('booking',{client_name:f.get('client_name'),client_phone:f.get('client_phone')||null,hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit,shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null});
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
 }else if(form.id==='workflowSale'){
  const b=data.bookings.find(b=>b.id===selected);if(!b)throw new Error('Выберите съёмку.');const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const photos=imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold>total)throw new Error('Проверьте количество кадров.');
  if(photos.length+(d?.selected||0)!==sold)throw new Error(`Нужно прикрепить ${sold-(d?.selected||0)} выбранных кадров.`);
  const draft=add('sale_start',{booking:{id:b.id},price:b.price});
  if(!d||d.status==='AWAITING_RECEIPT'){const receipt=imageFiles(form,'receipt',8*1024*1024)[0];if(!receipt)throw new Error('Прикрепите чек продажи.');add('sale_receipt',{draft},receipt);}
  if(!d||d.status!=='AWAITING_SELECTED')add('sale_counts',{draft,total,sold,price:b.price});
  for(const photo of photos)add('sale_selected',{draft},photo);
  add('sale_complete',{draft,price:b.price});
 }else{
  const b=data.bookings.find(b=>b.id===Number(f.get('booking')));if(!b)throw new Error('Выберите съёмку.');
  const photos=imageFiles(form,'photos');
  if(f.get('finish')){
   const pending=(await outboxRows()).some(r=>r.kind==='shoot_photo'&&r.data?.shooting?.id===b.shootingId&&r.status!=='synced');
   if(folderBusy||pending)throw new Error('Дождитесь отправки всех кадров и исправьте ошибки в очереди перед завершением загрузки.');
   if(photos.length)throw new Error('Сначала отправьте выбранные фотографии. Когда все кадры сохранятся на Яндекс.Диске, завершите загрузку отдельным действием.');
   if(!(b.files||[]).length||(b.files||[]).some(file=>file.status!=='STORED'))throw new Error('Завершение доступно, когда каждый кадр сохранён на Яндекс.Диске. Обновите состояние файлов или повторите неудачные загрузки.');
   add('shoot_complete',{shooting:{id:b.shootingId}});
  }else for(const photo of photos)add('shoot_photo',{shooting:{id:b.shootingId}},photo);
 }
 await enqueueBatch(items);form.reset();queue=await outboxRows();document.querySelector('#workflowQueue').innerHTML=queueView();
}
export async function workflowClick(button){
 const kind=button.dataset.workflow;
 if(kind.startsWith('edit-')){const action=kind.slice(5);await enqueueOperation('photo_edit',todayKey(),{shooting:Number(button.dataset.shooting),photo:Number(button.dataset.photo),action:action==='ai'?'AI':action,edit:button.dataset.edit?Number(button.dataset.edit):null,parameters:null});return;}
 if(kind==='compare'){const pid=Number(button.dataset.photo),eid=Number(button.dataset.edit);const blobs=await Promise.all([api(`/photos/${pid}/image`,{responseType:'blob'}),api(`/photos/${pid}/image?edit=${eid}`,{responseType:'blob'})]);const target=document.querySelector(`[data-compare="${eid}"]`);if(!target)return;clearComparisons();compareURLs=blobs.map(b=>URL.createObjectURL(b));target.innerHTML=compareURLs.map((url,i)=>`<figure><figcaption>${i?'Обработанная копия':'Оригинал'}</figcaption><img src="${url}" alt="${i?'Обработанная копия':'Оригинал'}" style="max-width:100%;height:auto"></figure>`).join('');return;}
if(kind==='sync'){
 const note=document.querySelector('#workflowSyncNote');
 if(note)note.textContent='Проверяем состояние очереди…';
 const result=await flushOutbox();
 queue=await outboxRows();
 const list=document.querySelector('#workflowQueue');if(list)list.innerHTML=queueView();
 scheduleStorageRefresh();
 const messages={'no-account':'Нет активного аккаунта. Войдите под тем же пользователем, который загружал файлы.',offline:'Браузер сообщает, что нет интернета. Фотографии остаются на устройстве.',running:'Загрузка уже выполняется. Статусы кадров обновляются в очереди.',done:`Проверка завершена. Принято сервером: ${result?.completed||0}; ошибок: ${result?.failed||0}; временных сбоев: ${result?.temporary||0}. Проверяйте статус файлов на Яндекс.Диске отдельно.`};
 if(note)note.textContent=messages[result?.state]||'Проверка завершена; обновите состояние файлов.';
 return;
}
if(button.dataset.workflow==='refresh'){await loadWorkflow();document.querySelector('#app').innerHTML=workflowView();return;}if(button.dataset.workflow==='retry-storage'){await enqueueOperation('shoot_retry',todayKey(),{shooting:{id:Number(button.dataset.shooting)}});return;}if(button.dataset.workflow==='retry')await retryOperation(button.dataset.key);else await flushOutbox();}
export function workflowSelect(id){selected=Number(id)||null;document.querySelector('#app').innerHTML=workflowView();}
function refreshConnection(){const el=document.querySelector('#workflowNotice');if(el)el.textContent=connectionNote();}
window.addEventListener('online',refreshConnection);window.addEventListener('offline',refreshConnection);
let storageTimer,storageLoading=false;
function scheduleStorageRefresh(delay=800){clearTimeout(storageTimer);storageTimer=setTimeout(refreshStorage,delay);}
async function refreshStorage(){
 if(storageLoading||document.hidden||!document.querySelector('#workflowFiles'))return;
 storageLoading=true;
 try{data=await api('/workflow');const panel=document.querySelector('#workflowFiles');if(panel){
   // Storage updates must not interrupt an exposure/WB form or comparison.
   if(panel.contains(document.activeElement)||compareURLs.length){scheduleStorageRefresh(5000);return;}
   const values=new Map([...panel.querySelectorAll('.photo-edit')].map(form=>[`${form.dataset.shooting}:${form.dataset.photo}`,Object.fromEntries(new FormData(form))]));
   const opened=[...panel.querySelectorAll('details')].map(d=>d.open);panel.outerHTML=storageView();
   document.querySelectorAll('#workflowFiles details').forEach((d,i)=>d.open=opened[i]||false);
   for(const form of document.querySelectorAll('#workflowFiles .photo-edit'))for(const [key,value] of Object.entries(values.get(`${form.dataset.shooting}:${form.dataset.photo}`)||{}))if(form.elements[key])form.elements[key].value=value;
  }}
 catch{}finally{storageLoading=false;}
 if(data?.bookings.some(b=>(b.files||[]).some(f=>['PENDING','UPLOADING'].includes(f.status)))||queue.some(r=>['shoot_photo','shoot_complete','shoot_retry'].includes(r.kind)&&['local','syncing'].includes(r.status)))scheduleStorageRefresh(5000);
}
window.addEventListener('pb-outbox',async()=>{refreshConnection();const el=document.querySelector('#workflowQueue');if(el){queue=await outboxRows();el.innerHTML=queueView();scheduleStorageRefresh();}});
window.addEventListener('hashchange',()=>{clearTimeout(storageTimer);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)scheduleStorageRefresh();});
