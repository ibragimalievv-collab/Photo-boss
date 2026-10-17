import {captureForm,captureForms,restoreForms,clearForm} from './form-state.js';
import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox,syncOutboxNow,enqueueOperation,enqueueFilesOneByOne,replaceOperationFile} from './outbox.js';
let data=null,queue=[],selected=null,folderBusy=false;
const roubles=value=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2}).format(Number(value||0));
const saleable=b=>b.status==='READY_FOR_SALE'||(['SOLD','COMPLETED'].includes(b.status)&&b.sales?.length);
const labels={local:'В очереди на этом устройстве — не отправлено',syncing:'Передаётся на сервер',synced:'Сервер принял',error:'Ошибка отправки'};
const kinds={booking:'Бронь',booking_receipt:'Чек брони',sale_start:'Черновик продажи',sale_receipt:'Чек продажи',sale_counts:'Количество кадров',sale_selected:'Выбранный кадр',sale_complete:'Оформление продажи',shoot_photo:'Кадр полной съёмки',shoot_complete:'Завершение загрузки',checklist:'Контрольный пункт',shift_report:'Замечание',attendance:'Отметка смены'};
export async function loadWorkflow(bookingId=null){
 const suffix=bookingId||selected;let next;
 try{next=await api('/workflow'+(suffix?'?booking='+Number(suffix):''));}
 catch(error){if(!suffix||![403,404].includes(error.status))throw error;selected=null;next=await api('/workflow');}
 if(data?.actorId!==next.actorId)selected=null;
 data=next;queue=await outboxRows();
 if(bookingId&&data.bookings.some(b=>b.id===Number(bookingId)))selected=Number(bookingId);
 if(!selected)try{selected=Number(sessionStorage.getItem('pb-workflow-sale:'+data.actorId))||null;}catch{}
 if(selected&&!data.bookings.some(b=>b.id===selected))selected=null;
}
function salesHistory(b){
 const rows=b.sales||[],receipts=b.receipts||[],draft=draftReceiptView(b.draft);if(!rows.length&&!receipts.length)return draft;
 return `<section class="notice" data-sales-history><h3>История покупок · ${esc(b.client||'Гость')}</h3>
 ${rows.map((sale,i)=>`<p>№${sale.id} · ${i?'Допродажа':'Первая покупка'} · ${esc(new Date(sale.at).toLocaleString('ru-RU'))} · ${sale.photos} фото · <strong>${esc(roubles(sale.amount))}</strong> · ${esc(({PAID:'Оплачено',PARTIAL:'Частично оплачено',UNPAID:'Ожидает оплаты'})[sale.paymentStatus]||sale.paymentStatus)}</p>`).join('')}
 <p><strong>Всего куплено: ${b.soldTotal} фото на ${esc(roubles(b.salesAmount))}.</strong><br>Подтверждено поступлений: ${esc(roubles(b.paidAmount))}. Остаток к оплате: ${esc(roubles(b.dueAmount))}.</p>
 <p>Предыдущие продажи и чеки не перезаписываются. Ниже укажите только дополнительные фотографии и новый чек.</p>
 ${receipts.map(receipt=>`<article data-receipt-status="${receipt.id}"><strong>Чек №${receipt.id}: ${receipt.stored?'файл принят сервером':'файл ещё не принят'}</strong>${receipt.imagePath?`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(receipt.imagePath)}">Посмотреть фото чека</button>`:''}<p>${esc(receiptAnalysisLabel(receipt.analysisStatus))}${receipt.extractedAmount!=null?` · на чеке: ${esc(roubles(receipt.extractedAmount))}`:''}${receipt.expectedAmount!=null?` · ожидается: ${esc(roubles(receipt.expectedAmount))}`:''}</p>${(receipt.warnings||[]).map(w=>`<p>${esc(w)}</p>`).join('')}<p>${esc(({PENDING:'Оплата пока не подтверждена: владелец должен сверить поступление с банковской выпиской.',APPROVED:'Поступление подтверждено владельцем.',REJECTED:'Чек отклонён владельцем.'})[receipt.status]||'Подтверждение оплаты отсутствует.')}</p></article>`).join('')}${draft}</section>`;
}
function receiptAnalysisLabel(status){return ({READY:'Данные чека распознаны AI; это не подтверждение поступления денег.',SUCCESS:'Данные чека распознаны AI; это не подтверждение поступления денег.',EXTRACTED:'Данные чека распознаны AI; это не подтверждение поступления денег.',UNAVAILABLE:'AI пока не прочитал чек; нужна повторная или ручная проверка.',FAILED:'AI пока не прочитал чек; нужна повторная или ручная проверка.',PENDING:'AI-проверка ожидается.'})[String(status||'').toUpperCase()]||'Результат AI-проверки пока отсутствует.';}
function draftReceiptView(d){if(!d)return '';return `<section data-draft-receipt><p>${d.receiptStored?'Файл чека принят сервером. '+esc(receiptAnalysisLabel(d.receiptAnalysisStatus)):'Файл чека ещё не принят сервером — проверьте очередь отправки ниже.'} Подтвердить оплату может только владелец после сверки поступления.</p>${d.receiptImagePath?`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(d.receiptImagePath)}">Посмотреть фото чека</button>`:''}</section>`;}
const field=(label,input)=>`<label class="field">${label}${input}</label>`;
const input=(name,attrs='')=>`<input class="input" name="${name}" ${attrs}>`;
const file=(name,multiple=false,required=true)=>input(name,`type="file" accept="image/jpeg,image/png,image/webp" ${multiple?'multiple':''} ${required?'required':''}`);
const bookingTitle=b=>`${b.hotel||'Отель не указан'} · ${b.date||''} ${b.time||''} · ${b.client||'Гость'} · ${b.phone||'Телефон не указан'} · комната ${b.room||'—'}`;
const options=rows=>rows.map(b=>`<option value="${b.id}">${esc(b.name||bookingTitle(b))}</option>`).join('');

const editStatus={PENDING:'В очереди',RUNNING:'Обрабатывается',READY:'Копия сохранена',FAILED:'Ошибка',CANCELLED:'Отменена'};
function editView(b){if(!b.canUpload)return '';return `<details><summary>Обработка · оригиналы сохраняются отдельно</summary><p>AI подбирает только свет и цвет. Ручные настройки доступны независимо от AI. Обработанные копии не заменяют оригиналы.</p>${(b.files||[]).filter(f=>f.status==='STORED').map(f=>`<details><summary>Кадр №${f.id}</summary><form class="form-grid photo-edit" data-shooting="${b.shootingId}" data-photo="${f.id}">${[['exposure','Экспозиция (EV)',0,-1.5,1.5],['contrast','Контраст',1,.8,1.2],['color','Насыщенность',1,.8,1.2],['red','Баланс белого: красный',1,.85,1.15],['blue','Баланс белого: синий',1,.85,1.15]].map(([key,label,val,min,max])=>field(label,input(key,`type="number" value="${val}" min="${min}" max="${max}" step="0.05" required`))).join('')}<button class="btn primary">Создать обработанную копию</button></form><button class="btn ghost" data-workflow="edit-ai" data-shooting="${b.shootingId}" data-photo="${f.id}">Подобрать свет и цвет с AI</button>${(b.edits||[]).filter(e=>e.photo===f.id).map(e=>`<article><p>Версия №${e.id}: ${esc(editStatus[e.status])} ${esc(e.error||'')}</p>${e.status==='READY'?`<button class="btn ghost" data-workflow="compare" data-photo="${f.id}" data-edit="${e.id}">Сравнить с оригиналом</button>`:''}${e.status==='FAILED'?`<button class="btn ghost" data-workflow="edit-retry" data-shooting="${b.shootingId}" data-photo="${f.id}" data-edit="${e.id}">Повторить обработку</button>`:''}${e.status!=='CANCELLED'?`<button class="btn ghost" data-workflow="edit-cancel" data-shooting="${b.shootingId}" data-photo="${f.id}" data-edit="${e.id}">Отменить результат</button>`:''}<div data-compare="${e.id}"></div></article>`).join('')}</details>`).join('')}</details>`;}
let compareURLs=[];
function clearComparisons(){for(const url of compareURLs)URL.revokeObjectURL(url);compareURLs=[];}
window.addEventListener('hashchange',clearComparisons);window.addEventListener('pagehide',clearComparisons);
let receiptViewer=null,receiptURL=null,receiptVersion=0;
function clearReceiptViewer(){
 receiptVersion++;const viewer=receiptViewer;receiptViewer=null;
 if(receiptURL){URL.revokeObjectURL(receiptURL);receiptURL=null;}
 if(viewer?.open)viewer.close();viewer?.remove();
}
window.addEventListener('hashchange',clearReceiptViewer);window.addEventListener('pagehide',clearReceiptViewer);
async function viewReceipt(path){
 if(!/^\/(?:receipts\/\d+\/image|sale-drafts\/\d+\/receipt\/image)$/.test(path||''))throw new Error('Некорректная ссылка на чек.');
 clearReceiptViewer();const ticket=receiptVersion,blob=await api(path,{responseType:'blob'});
 if(ticket!==receiptVersion)return;
 const viewer=document.createElement('dialog'),image=document.createElement('img'),close=document.createElement('button');
 viewer.className='delivery-viewer';viewer.setAttribute('aria-label','Фото чека');
 close.type='button';close.className='btn';close.textContent='Закрыть';close.onclick=()=>viewer.close();
 receiptURL=URL.createObjectURL(blob);image.src=receiptURL;image.alt='Фото загруженного чека';
 viewer.append(close,image);viewer.addEventListener('close',clearReceiptViewer,{once:true});
 receiptViewer=viewer;document.body.append(viewer);viewer.showModal();
}

function replacementControl(r){const rejected=[400,413].includes(r.errorStatus)||(r.kind==='sale_selected'&&r.errorStatus===409&&r.error==='Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.');return r.status==='error'&&rejected&&r.hasBlob&&['shoot_photo','delivery_photo','sale_selected','booking_receipt','sale_receipt'].includes(r.kind)?`<label class="field">Исправить отклонённый файл без новой продажи<input class="input" type="file" accept="image/jpeg,image/png,image/webp" data-workflow-replace="${r.key}"></label>`:'';}
function queueView(){
 const inProgress=queue.filter(r=>r.status==='local'||r.status==='syncing').length;
 const errors=queue.filter(r=>r.status==='error').length;
 const stored=queue.filter(r=>r.status==='synced').length;
 const visible=[...queue.filter(r=>r.status!=='synced').slice(0,50),...queue.filter(r=>r.status==='synced').slice(-20)];
 const summary=`<p class="small">Ожидают отправки или отправляются: ${inProgress} · Ошибки: ${errors} · Принято сервером: ${stored}. Показаны первые 50 незавершённых и последние 20 завершённых операций.</p>`;
 return summary+(visible.map(r=>`<article class="panel"><strong>${r.kind==='shoot_photo'&&r.status==='synced'?'Сервер принял · проверьте Яндекс.Диск ниже':labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.photoId?` · кадр №${r.result.photoId}`:''}${r.filename?` · ${esc(r.filename)}`:''}${r.data?.shooting?.id?` · съёмка №${r.data.shooting.id}`:''}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.retryAt>Date.now()?`<p>Автоматический повтор не раньше ${esc(new Date(r.retryAt).toLocaleTimeString('ru-RU'))}. Остальные файлы отправляются независимо.</p>`:''}${r.status==='error'||(r.status==='local'&&r.error)?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">Повторить</button>`:''}${replacementControl(r)}</article>`).join('')||'<p>Операций в очереди нет.</p>');
}
function connectionNote(){
 const offline=data?._offline?(`${navigator.onLine?'Сеть доступна, но справочники взяты из локальной копии.':'Нет сети. Справочники сохранены.'} ${new Date(data._cachedAt).toLocaleString('ru-RU')}. `):'';
 return offline+'Фотографии сначала попадают в очередь именно этого браузера на этом устройстве. Пока сервер не принял их, не очищайте данные браузера. После передачи проверяйте отдельный статус «Сохранено на Яндекс.Диске».';
}
export function workflowView(){
 const ready=data.bookings.filter(saleable),b=data.bookings.find(b=>b.id===selected),d=b?.draft;
 const upload=data.bookings.filter(b=>b.canFullUpload??(b.status==='READY_FOR_SALE'&&!b.fullUploaded&&b.canUpload));
 return `<h1>Рабочие операции</h1><div id="workflowNotice" class="notice">${esc(connectionNote())}</div>
 ${data.canBook?`<details class="panel section"><summary>Новая бронь</summary><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Заявленная предоплата, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="0" required'))}${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа и допродажа</h2><form id="workflowLookup" class="row"><label>Номер записи <input class="input" name="bookingId" type="number" min="1" required></label><button class="btn ghost" type="submit">Найти прежнюю съёмку</button></form><p>Выбранные кадры обязательны; оплата подтверждается владельцем.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(bookingTitle(x))}</option>`).join('')}</select>${b?salesHistory(b):''}${b?(d&&!d.mine?'<p>Продажу оформляет другой сотрудник.</p>':`<form id="workflowSale" data-draft-scope="${b.id}" class="form-grid section"><p>${b.sales?.length?'Допродажа: укажите только новую покупку. ':''}Цена кадра по текущему пакету: ${esc(b.price)} ₽. Итог рассчитает сервер.</p>${!d||d.status==='AWAITING_RECEIPT'?field(b.sales?.length?'Новый чек доплаты (не прежний чек)':'Чек продажи',file('receipt')):''}${field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${d?.total||b.totalFrames||''}" ${d?.status==='AWAITING_SELECTED'||b.fullUploaded?'readonly':''}`))}${field(b.sales?.length?'Дополнительно продано кадров':'Продано кадров',input('sold',`type="number" min="1" max="10000" required value="${d?.sold||''}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}<p>Уже сохранено выбранных кадров: ${d?.selected||0}. Прикрепите оставшиеся. После смены Диска можно повторно выбрать все купленные кадры: дубликаты восстановятся без новой продажи.</p>${field('Выбранные фотографии',file('selected',true,false))}<button class="btn primary">${b.sales?.length?'Добавить допродажу':'Сохранить продажу и фотографии'}</button></form>`):''}</section>
 <section class="panel section"><h2>Продажи по съёмкам</h2>${data.bookings.filter(x=>x.sales?.length).map(x=>`<article class="section"><strong>№${x.id} · ${esc(x.client||'Гость')} · ${esc(x.date)}</strong><p>${x.soldTotal} фото · ${esc(roubles(x.salesAmount))}</p><button class="btn ghost" data-workflow="sale-history" data-booking="${x.id}">Редактировать / добавить покупку</button></article>`).join('')||'<p>Завершённых покупок пока нет.</p>'}</section>
 <details class="panel section"><summary>Загрузить полную съёмку</summary>${upload.length?`<form id="workflowShoot" class="form-grid">${field('Съёмка',`<select class="input" name="booking">${options(upload)}</select>`)}${field('Фотографии',file('photos',true,false))}<label><input type="checkbox" name="finish"> Все кадры добавлены — завершить загрузку</label><p>Можно добавлять частями. Ставка фотографа фиксируется сервером после завершения полной загрузки.</p><button class="btn primary">Сохранить фотографии</button></form><label class="field section"><span>Выбрать папку — отправка сразу, по одному файлу</span><input id="workflowShootFolder" class="input" type="file" accept="image/jpeg,image/png,image/webp" webkitdirectory multiple></label><p id="workflowFolderProgress" class="notice" role="status">После выбора папки не закрывайте вкладку до конца передачи. Проверяйте Яндекс.Диск отдельно.</p>`:'<p>Нет доступных съёмок для полной загрузки.</p>'}</details>
 <section class="panel section"><h2>Файлы на Яндекс.Диске</h2><button class="btn ghost" data-workflow="refresh">Обновить состояние файлов</button>${data.bookings.filter(b=>b.shootingId).map(b=>`<details data-storage-shooting="${b.shootingId}"><summary>${esc(bookingTitle(b))} · сохранено ${(b.files||[]).filter(f=>f.status==='STORED').length} из ${(b.files||[]).length}</summary>${(b.files||[]).map(f=>`<p>Кадр №${f.id}: ${esc(({STORED:'Сохранено на Диске',PENDING:'Ожидает сохранения',UPLOADING:'Сохраняется',FAILED:'Ошибка сохранения'})[f.status])} ${esc(f.error||'')}</p>`).join('')}${editView(b)}${b.canUpload?`<button class="btn ghost" data-workflow="retry-storage" data-shooting="${b.shootingId}">Восстановить сохранение на Диске</button>`:''}</details>`).join('')||'<p>Съёмок пока нет.</p>'}</section>
 <section class="section"><h2>Синхронизация</h2><button class="btn ghost" data-workflow="sync">Синхронизировать сейчас</button><div id="workflowSyncNote" class="notice" role="status">Отправка запускается автоматически при наличии сети. Нажмите кнопку для ручной проверки.</div><div id="workflowQueue">${queueView()}</div></section>`;
}
function imageFiles(form,name,maximum=20*1024*1024){const list=[...form.elements[name].files];for(const f of list)if(!['image/jpeg','image/png','image/webp'].includes(f.type)||!f.size||f.size>maximum)throw new Error(`Изображения JPEG, PNG или WebP, не больше ${maximum/1024/1024} МБ.`);return list;}
export async function workflowFolderChange(input){
 if(folderBusy)throw new Error('Другая папка уже загружается.');
 const form=document.querySelector('#workflowShoot');
 const booking=data?.bookings.find(b=>b.id===Number(form?.elements.booking.value));
 if(!booking?.canUpload||!booking.shootingId)throw new Error('Выберите доступную съёмку.');
 const chosen=[...input.files];
 const files=chosen.filter(f=>(['image/jpeg','image/png','image/webp'].includes(f.type)||(!f.type&&/\.(jpe?g|png|webp)$/i.test(f.name)))&&f.size>0&&f.size<=20*1024*1024);
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
 captureForm(form);
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=(await outboxRows()).filter(r=>keys.includes(r.key));
  if(rows.length!==keys.length)throw new Error('Часть очереди не найдена. Не создавайте повторную продажу; проверьте журнал синхронизации.');
  if(rows.every(r=>r.status==='synced'))throw new Error('Эта операция уже сохранена. Для новой используйте кнопку под результатом.');
  for(const row of rows.filter(r=>r.status==='error'||r.error))await retryOperation(row.key);
  await flushOutbox();queue=await outboxRows();updateFormProgress();return;
 }
 const f=new FormData(form),date=todayKey(),items=[],add=(kind,data,blob=null)=>{const row={key:crypto.randomUUID(),kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.classList.contains('photo-edit')){add('photo_edit',{shooting:Number(form.dataset.shooting),photo:Number(form.dataset.photo),action:'MANUAL',edit:null,parameters:Object.fromEntries(['exposure','contrast','color','red','blue'].map(k=>[k,Number(f.get(k))]))});
 }else if(form.id==='workflowBooking'){
  const receipt=imageFiles(form,'receipt',8*1024*1024)[0],deposit=f.get('deposit');if(Number(deposit)>0&&!receipt)throw new Error('Прикрепите чек заявленной предоплаты.');
  const booking=add('booking',{client_name:f.get('client_name'),client_phone:f.get('client_phone')||null,hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit,shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null});
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
 }else if(form.id==='workflowSale'){
  const b=data.bookings.find(b=>b.id===selected);if(!b)throw new Error('Выберите съёмку.');const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const photos=imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте количество кадров.');
  if(photos.length>sold||photos.length+(d?.selected||0)<sold)throw new Error(`Нужно прикрепить ${sold-(d?.selected||0)} выбранных кадров.`);
  const draft=add('sale_start',{booking:{id:b.id},price:b.price});
  if(!d||d.status==='AWAITING_RECEIPT'){const receipt=imageFiles(form,'receipt',8*1024*1024)[0];if(!receipt)throw new Error('Прикрепите чек продажи.');add('sale_receipt',{draft},receipt);}
  if(!d||d.status!=='AWAITING_SELECTED')add('sale_counts',{draft,total,sold,price:b.price});
  for(const photo of photos)add('sale_selected',{draft},photo);
  add('sale_complete',{draft,price:b.price});
 }else{
  const b=data.bookings.find(b=>b.id===Number(f.get('booking')));if(!b)throw new Error('Выберите съёмку.');
  for(const photo of imageFiles(form,'photos'))add('shoot_photo',{shooting:{id:b.shootingId}},photo);
  if(f.get('finish'))add('shoot_complete',{shooting:{id:b.shootingId}});
 }
 await enqueueBatch(items);
 form.dataset.pendingKeys=JSON.stringify(items.map(row=>row.key));captureForm(form);
 queue=await outboxRows();const list=document.querySelector('#workflowQueue');if(list)list.innerHTML=queueView();updateFormProgress();
}
export async function workflowClick(button){
 const kind=button.dataset.workflow;
 if(kind==='receipt-view'){await viewReceipt(button.dataset.receiptPath);return;}
 if(kind==='sale-history'){await loadWorkflow(Number(button.dataset.booking));workflowSelect(button.dataset.booking);document.querySelector('#workflowSale')?.scrollIntoView({block:'center'});return;}
 if(kind==='new-operation'){const form=button.closest('form');if(!form)return;const keys=JSON.parse(form.dataset.pendingKeys||'[]');const rows=await outboxRows();if(!keys.length||!keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced')))throw new Error('Сначала завершите текущую отправку.');clearForm(form);form.reset();form.remove();await loadWorkflow();captureForms();document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();return;}

 if(kind.startsWith('edit-')){const action=kind.slice(5);await enqueueOperation('photo_edit',todayKey(),{shooting:Number(button.dataset.shooting),photo:Number(button.dataset.photo),action:action==='ai'?'AI':action,edit:button.dataset.edit?Number(button.dataset.edit):null,parameters:null});return;}
 if(kind==='compare'){const pid=Number(button.dataset.photo),eid=Number(button.dataset.edit);const blobs=await Promise.all([api(`/photos/${pid}/image`,{responseType:'blob'}),api(`/photos/${pid}/image?edit=${eid}`,{responseType:'blob'})]);const target=document.querySelector(`[data-compare="${eid}"]`);if(!target)return;clearComparisons();compareURLs=blobs.map(b=>URL.createObjectURL(b));target.innerHTML=compareURLs.map((url,i)=>`<figure><figcaption>${i?'Обработанная копия':'Оригинал'}</figcaption><img src="${url}" alt="${i?'Обработанная копия':'Оригинал'}" style="max-width:100%;height:auto"></figure>`).join('');return;}
if(kind==='sync'){
 const note=document.querySelector('#workflowSyncNote');
 if(note)note.textContent='Проверяем состояние очереди…';
 const result=await syncOutboxNow();
 queue=await outboxRows();
 const list=document.querySelector('#workflowQueue');if(list)list.innerHTML=queueView();
 const messages={'no-account':'Нет активного аккаунта. Войдите под тем же пользователем, который загружал файлы.',offline:'Браузер сообщает, что нет интернета. Фотографии остаются на устройстве.',running:'Загрузка уже выполняется. Статусы кадров обновляются в очереди.',done:`Проверка завершена. Принято сервером: ${result?.completed||0}; ошибок: ${result?.failed||0}; временных сбоев: ${result?.temporary||0}. Проверяйте статус файлов на Яндекс.Диске отдельно.`};
 if(note)note.textContent=messages[result?.state]||'Проверка завершена; обновите состояние файлов.';
 return;
}
if(button.dataset.workflow==='refresh'){captureForms();await loadWorkflow();document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();return;}if(button.dataset.workflow==='retry-storage'){await enqueueOperation('shoot_retry',todayKey(),{shooting:{id:Number(button.dataset.shooting)}});return;}if(button.dataset.workflow==='retry')await retryOperation(button.dataset.key);else await flushOutbox();}
export function workflowSelect(id){captureForms();selected=Number(id)||null;try{sessionStorage.setItem('pb-workflow-sale:'+data.actorId,String(selected||''));}catch{}document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();}
function refreshConnection(){const el=document.querySelector('#workflowNotice');if(el)el.textContent=connectionNote();}
window.addEventListener('online',refreshConnection);window.addEventListener('offline',refreshConnection);
window.addEventListener('pb-outbox',async()=>{refreshConnection();const el=document.querySelector('#workflowQueue');if(el){queue=await outboxRows();el.innerHTML=queueView();updateFormProgress();}});

function updateFormProgress(){
 for(const form of document.querySelectorAll('#workflowBooking,#workflowSale,#workflowShoot')){
  if(!form.dataset.pendingKeys)continue;
  let keys;try{keys=JSON.parse(form.dataset.pendingKeys);}catch{continue;}
  const rows=keys.map(key=>queue.find(row=>row.key===key)),done=rows.filter(r=>r?.status==='synced').length;
  const last=rows[rows.length-1],finished=done===keys.length&&keys.length>0;
  let note=form.querySelector('[data-operation-progress]');if(!note){note=document.createElement('div');note.dataset.operationProgress='';note.setAttribute('role','status');form.append(note);}
  const failed=rows.find(r=>r?.error);
  note.textContent=finished?(last?.result?.saleId?`Продажа №${last.result.saleId} сохранена сервером.`:'Операции приняты сервером. Для оригиналов отдельно проверьте статус на Яндекс.Диске.'):`В очереди сохранены все данные. Сервер принял ${done} из ${keys.length}. ${failed?.error||'Отправка продолжается.'} Повторное заполнение не требуется.`;
  for(const el of [...form.elements])if(!['button','submit'].includes(el.type))el.disabled=true;
  const button=form.querySelector('button[type="submit"],button:not([type])');
  if(button){button.disabled=finished;button.textContent=finished?'Сохранено':failed?'Повторить отправку сохранённых данных':'Проверить отправку';}
  if(finished&&!form.querySelector('[data-workflow="new-operation"]')){const next=document.createElement('button');next.type='button';next.className='btn ghost';next.dataset.workflow='new-operation';next.textContent='Начать новую операцию';form.append(next);}
 }
}
window.addEventListener('pb-workflow-mounted',updateFormProgress);
document.addEventListener('change',async event=>{
 const input=event.target;if(!input?.matches?.('[data-workflow-replace]')||!input.files?.length)return;
 input.disabled=true;
 try{await replaceOperationFile(input.dataset.workflowReplace,input.files[0]);}
 catch(error){const note=document.querySelector('#workflowSyncNote');if(note)note.textContent=error.message;}
 finally{if(input.isConnected){input.disabled=false;input.value='';}}
});

document.addEventListener('submit',async event=>{
 if(event.target.id!=='workflowLookup')return;event.preventDefault();const form=event.target,button=form.querySelector('button');button.disabled=true;
 try{const id=Number(new FormData(form).get('bookingId'));captureForms();await loadWorkflow(id);if(!data.bookings.some(b=>b.id===id))throw Error('Съёмка не найдена или недоступна.');workflowSelect(id);}
 catch(error){const el=document.querySelector('#workflowNotice');if(el)el.textContent=error.message;}finally{button.disabled=false;}
});
