import {bankPanel} from './bank-payments.js';
import {pricingView,tariffOptions,updatePricePreview} from './hotel-pricing.js';
import {captureForm,captureForms,restoreForms,clearForm} from './form-state.js';
import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox,syncOutboxNow,enqueueOperation,enqueueFilesOneByOne,replaceOperationFile,canReplaceOperationFile,saleQueueRepairable,repairSaleQueue,rejectedSaleCounts} from './outbox.js';
import {checkSaleFiles} from './sale-files.js';
let data=null,queue=[],selected=null,folderBusy=false;
const roubles=value=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2}).format(Number(value||0));
const frameTotal=b=>b.fullUploaded?(b.uploadedOriginals??b.totalFrames):(b.draft?.total||b.totalFrames||'');
const saleable=b=>Boolean(b.draft)||b.status==='READY_FOR_SALE'||(['SOLD','COMPLETED'].includes(b.status)&&b.sales?.length);
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
 ${rows.map((sale,i)=>`<p>№${sale.id} · ${i?'Допродажа':'Первая покупка'} · ${esc(new Date(sale.at).toLocaleString('ru-RU'))} · ${sale.photos} фото${(sale.lines||[]).map(line=>` · ${line.type==='PACKAGE'?`пакет «${esc(line.name)}»`: `поштучно ${esc(line.price)} ₽ × ${line.quantity}`} = ${esc(roubles(line.amount))}`).join('')} · <strong>${esc(roubles(sale.amount))}</strong> · ${esc(({PAID:'Оплачено',PARTIAL:'Частично оплачено',UNPAID:'Ожидает оплаты'})[sale.paymentStatus]||sale.paymentStatus)}</p>`).join('')}
 <p><strong>Всего куплено: ${b.soldTotal} фото на ${esc(roubles(b.salesAmount))}.</strong><br>Подтверждено поступлений: ${esc(roubles(b.paidAmount))}. Остаток к оплате: ${esc(roubles(b.dueAmount))}.</p>
 <p>Предыдущие продажи и чеки не перезаписываются. Ниже укажите только дополнительные фотографии и новый чек.</p>
 ${receipts.map(receipt=>`<article data-receipt-status="${receipt.id}"><strong>Чек №${receipt.id}: ${receipt.source==='sber'?'поступление подтверждено банком':receipt.stored?'файл принят сервером':'файл ещё не принят'}</strong>${receipt.imagePath?`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(receipt.imagePath)}">Посмотреть фото чека</button>`:''}<p>${esc(receiptAnalysisLabel(receipt.analysisStatus))}${receipt.extractedAmount!=null?` · на чеке: ${esc(roubles(receipt.extractedAmount))}`:''}${receipt.expectedAmount!=null?` · ожидается: ${esc(roubles(receipt.expectedAmount))}`:''}</p>${(receipt.warnings||[]).map(w=>`<p>${esc(w)}</p>`).join('')}<p>${esc(({PENDING:'Оплата пока не подтверждена: владелец должен сверить поступление с банковской выпиской.',APPROVED:receipt.source==='sber'?'Поступление подтверждено СберБизнес.':'Поступление подтверждено владельцем.',REJECTED:'Чек отклонён владельцем.'})[receipt.status]||'Подтверждение оплаты отсутствует.')}</p></article>`).join('')}${draft}</section>`;
}
function receiptAnalysisLabel(status){return ({READY:'Данные чека распознаны AI; это не подтверждение поступления денег.',SUCCESS:'Данные чека распознаны AI; это не подтверждение поступления денег.',EXTRACTED:'Данные чека распознаны AI; это не подтверждение поступления денег.',UNAVAILABLE:'AI пока не прочитал чек; нужна повторная или ручная проверка.',FAILED:'AI пока не прочитал чек; нужна повторная или ручная проверка.',PENDING:'AI-проверка ожидается.'})[String(status||'').toUpperCase()]||'Результат AI-проверки пока отсутствует.';}
function draftReceiptView(d){if(!d)return '';return `<section data-draft-receipt><p>${d.receiptStored?'Файл чека принят сервером. '+esc(receiptAnalysisLabel(d.receiptAnalysisStatus)):'Файл чека ещё не принят сервером — проверьте очередь отправки ниже.'} Подтвердить оплату может только владелец после сверки поступления.</p>${d.receiptImagePath?`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(d.receiptImagePath)}">Посмотреть фото чека</button>`:''}</section>`;}
function draftProgress(b){
 const d=b.draft;if(!d)return '';
 const sold=Number(d.sold||0),saved=Number(d.selected||0),remaining=Math.max(0,sold-saved);
 const next=d.status==='AWAITING_RECEIPT'?'Прикрепите чек продажи.':d.status==='AWAITING_COUNTS'?'Укажите общее и проданное количество кадров.':saved>sold?`Принято ${saved} фото при указанном количестве ${sold}. Проверьте и исправьте количество проданных кадров; файлы сохранены.`:remaining?`Добавьте недостающие выбранные фотографии: ${remaining}.`:'Все выбранные фотографии приняты. Завершите продажу.';
 return `<section class="notice" data-sale-draft="${d.id}"><strong>Оформление продажи не завершено</strong><p>Всего кадров: ${frameTotal(b)||'не указано'} · Продано: ${sold||'не указано'} · Выбранных фото принято: ${saved} из ${sold||'—'}.</p>${d.amount!=null?`<p>Сумма продажи: <strong>${esc(roubles(d.amount))}</strong>${d.expectedAmount!=null?` · Ожидаемая доплата: ${esc(roubles(d.expectedAmount))}`:''}.</p>`:''}<p>${esc(next)}</p>${!d.mine?`<p>Оформляет: ${esc(d.createdByName||'другой сотрудник')}. Продолжить должен сотрудник, начавший продажу.</p>`:''}</section>`;
}
function salesOverview(){
 const bookings=data.bookings.filter(b=>b.draft||b.sales?.length),sales=bookings.flatMap(b=>b.sales||[]);
 const total=sales.reduce((n,s)=>n+Math.round(Number(s.amount||0)*100),0)/100,frames=sales.reduce((n,s)=>n+Number(s.photos||0),0);
 return `<h2>Продажи по съёмкам</h2><p data-sales-totals>В загруженных съёмках: завершённых продаж — ${sales.length}, продано кадров — ${frames}, сумма — ${esc(roubles(total))}. Незавершённых продаж — ${bookings.filter(b=>b.draft).length}.</p>${bookings.map(b=>`<article class="section" data-sale-booking="${b.id}"><strong>№${b.id} · ${esc(bookingTitle(b))}</strong>${b.sales?.length?`<p>Продажа оформлена: ${b.soldTotal} фото · ${esc(roubles(b.salesAmount))}.</p>`:''}${draftProgress(b)}<button class="btn ghost" data-workflow="sale-history" data-booking="${b.id}">${b.draft?'Продолжить продажу':'Редактировать / добавить покупку'}</button></article>`).join('')||'<p>Продаж пока нет.</p>'}`;
}
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

function replacementControl(r){return canReplaceOperationFile(r)&&r.hasBlob?`<label class="field">${r.kind==='sale_receipt'?'Прикрепить другой чек к этой же продаже':'Исправить отклонённый файл без новой продажи'}<input class="input" type="file" accept="image/jpeg,image/png,image/webp" data-workflow-replace="${r.key}"></label>`:'';}
function queueView(){
 const inProgress=queue.filter(r=>r.status==='local'||r.status==='syncing').length;
 const errors=queue.filter(r=>r.status==='error').length;
 const stored=queue.filter(r=>r.status==='synced').length;
 const visible=[...queue.filter(r=>r.status!=='synced').slice(0,50),...queue.filter(r=>r.status==='synced').slice(-20)];
 const summary=`<p class="small">Ожидают отправки или отправляются: ${inProgress} · Ошибки: ${errors} · Принято сервером: ${stored}. Показаны первые 50 незавершённых и последние 20 завершённых операций.</p>`;
 return summary+(visible.map(r=>`<article class="panel"><strong>${r.kind==='shoot_photo'&&r.status==='synced'?'Сервер принял · проверьте Яндекс.Диск ниже':labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.photoId?` · кадр №${r.result.photoId}`:''}${r.filename?` · ${esc(r.filename)}`:''}${r.data?.shooting?.id?` · съёмка №${r.data.shooting.id}`:''}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.retryAt>Date.now()?`<p>Автоматический повтор не раньше ${esc(new Date(r.retryAt).toLocaleTimeString('ru-RU'))}. Остальные файлы отправляются независимо.</p>`:''}${r.status==='error'||(r.status==='local'&&r.error)?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">Повторить</button>`:''}${replacementControl(r)}</article>`).join('')||'<p>Операций в очереди нет.</p>');
}
function saleTransferProgress(rows){
 const photos=rows.filter(row=>row?.kind==='sale_selected');if(!photos.length)return '';
 const accepted=photos.filter(row=>row.status==='synced'),ids=new Set(accepted.map(row=>row.result?.photoId).filter(Number.isSafeInteger));
 const reported=rows.filter(row=>row?.status==='synced'&&Number.isSafeInteger(row.result?.selectedPhotos)).map(row=>row.result.selectedPhotos);
 const confirmed=reported.length?`Сервер подтвердил в продаже разных кадров: ${Math.max(...reported)}.`:`В этой отправке подтверждено разных кадров: ${ids.size}.`;
 return `${confirmed} Файлов принято ${accepted.length} из ${photos.length}; повторных копий: ${accepted.filter(row=>row.result?.duplicate).length}. Ожидают отправки: ${photos.filter(row=>row.status==='local').length}; передаются: ${photos.filter(row=>row.status==='syncing').length}; ошибки файлов: ${photos.filter(row=>row.status==='error').length}.`;
}
function connectionNote(){
 const offline=data?._offline?(`${navigator.onLine?'Сеть доступна, но справочники взяты из локальной копии.':'Нет сети. Справочники сохранены.'} ${new Date(data._cachedAt).toLocaleString('ru-RU')}. `):'';
 return offline+'При закрытии или сворачивании приложения телефон может приостановить передачу. После открытия отправка продолжится из сохранённой очереди. Фотографии сначала попадают в очередь именно этого браузера на этом устройстве. Пока сервер не принял их, не очищайте данные браузера. После передачи проверяйте отдельный статус «Сохранено на Яндекс.Диске».';
}
export function workflowView(){
 const ready=data.bookings.filter(saleable),b=data.bookings.find(b=>b.id===selected),d=b?.draft;
 const upload=data.bookings.filter(b=>b.canFullUpload??(b.status==='READY_FOR_SALE'&&!b.fullUploaded&&b.canUpload));
 return `<h1>Рабочие операции</h1><div id="workflowNotice" class="notice">${esc(connectionNote())}</div>
 ${pricingView(data)}${data.canBook?`<details class="panel section"><summary>Новая бронь</summary><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Заявленная предоплата, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="0" required'))}${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа и допродажа</h2><form id="workflowLookup" class="row"><label>Номер записи <input class="input" name="bookingId" type="number" min="1" required></label><button class="btn ghost" type="submit">Найти прежнюю съёмку</button></form><p>Выберите поштучный тариф 300/400/500 ₽ за кадр или пакет этого отеля. Сумма и остаток показываются до подтверждения; поступившая предоплата учитывается только после подтверждения владельцем.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(bookingTitle(x))}</option>`).join('')}</select><div id="workflowSelectedSaleSummary">${b?salesHistory(b)+draftProgress(b):''}</div>${b?(d&&!d.mine?'<p>Продажу оформляет другой сотрудник.</p>':`<form id="workflowSale" data-draft-scope="${b.id}" class="form-grid section"><p>${b.sales?.length?'Допродажа: укажите только новую покупку. ':''}Количество кадров можно указать любым целым числом в пределах фактически загруженной съёмки. Сервер проверит число и тариф повторно.</p>${!d||d.status==='AWAITING_RECEIPT'?field(b.sales?.length?'Новый чек доплаты (не прежний чек)':'Чек продажи',file('receipt',false,false)):''}${tariffOptions(b,data.tariffs)}${field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${frameTotal(b)}" ${b.fullUploaded?'readonly':''}`))}${field(b.sales?.length?'Дополнительно продано кадров':'Продано кадров',input('sold',`type="number" min="1" max="10000" required value="${d?.sold||''}`))}<p>Уже сохранено выбранных кадров: ${d?.selected||0}. Прикрепите оставшиеся.${d?.status==='AWAITING_SELECTED'?' Если количество проданных кадров указано ошибочно, исправьте его выше: чек и принятые фото сохранятся. Уменьшить количество ниже уже принятых фото нельзя.':''} После смены Диска можно повторно выбрать все купленные кадры: дубликаты восстановятся без новой продажи.</p>${field('Выбранные фотографии',file('selected',true,false))}<button class="btn primary">${b.sales?.length?'Добавить допродажу':'Сохранить продажу и фотографии'}</button></form>`):''}</section>
 ${bankPanel(b,data)}<section class="panel section" id="workflowSalesSummary">${salesOverview()}</section>
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
  if(form.id==='workflowSale'&&form.dataset.repairing==='1'){
   const b=data.bookings.find(b=>b.id===selected),d=b?.draft,total=Number(form.elements.total.value),sold=Number(form.elements.sold.value),photos=imageFiles(form,'selected');
   const rejected=rejectedSaleCounts(rows);
   if(!d?.mine||!(rejected?['AWAITING_COUNTS','AWAITING_SELECTED'].includes(d.status):d.status==='AWAITING_SELECTED')||!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте фактическое количество кадров и доступ к продаже.');
   if(sold<d.selected)throw new Error(`Уже принято ${d.selected} выбранных фото. Проданное количество не может быть меньше.`);
   if(!rejected&&(photos.length>sold||photos.length+Number(d.selected||0)<sold))throw new Error(`Не хватает ${Math.max(0,sold-Number(d.selected||0))} новых выбранных кадров. Повторные фотографии не увеличивают количество.`);
   await checkSaleFiles(photos);
   const added=await repairSaleQueue(keys,{draftId:d.id,total,sold,price:b.price},photos);
   form.dataset.pendingKeys=JSON.stringify([...keys,...added.map(row=>row.key)]);delete form.dataset.repairing;captureForm(form);
   queue=await outboxRows();updateFormProgress();return;
  }
  for(const row of rows.filter(r=>r.status==='error'||r.error))await retryOperation(row.key);
  await flushOutbox();queue=await outboxRows();updateFormProgress();return;
 }
 const f=new FormData(form),date=todayKey(),items=[],add=(kind,data,blob=null)=>{const row={key:crypto.randomUUID(),kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.classList.contains('photo-edit')){add('photo_edit',{shooting:Number(form.dataset.shooting),photo:Number(form.dataset.photo),action:'MANUAL',edit:null,parameters:Object.fromEntries(['exposure','contrast','color','red','blue'].map(k=>[k,Number(f.get(k))]))});
 }else if(form.id==='workflowBooking'){
  const receipt=imageFiles(form,'receipt',8*1024*1024)[0],deposit=f.get('deposit');if(Number(deposit)>0&&!receipt&&!data.bankConfigured)throw new Error('Прикрепите чек заявленной предоплаты.');
  const booking=add('booking',{client_name:f.get('client_name'),client_phone:f.get('client_phone')||null,hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit,shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null});
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
 }else if(form.id==='workflowSale'){
  const b=data.bookings.find(b=>b.id===selected);if(!b)throw new Error('Выберите съёмку.');const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const photos=imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте количество кадров.');
  if(sold<(d?.selected||0))throw new Error(`Уже принято ${d.selected} выбранных фото. Проданное количество не может быть меньше.`);
  if(photos.length>sold||photos.length+(d?.selected||0)<sold)throw new Error(`Нужно прикрепить ${Math.max(0,sold-(d?.selected||0))} новых выбранных кадров. Повторные фотографии не увеличивают количество.`);
  await checkSaleFiles(photos);
  const startData={booking:{id:b.id},price:b.price},startTariff=d?.pricing?.tariffId||Number(f.get('tariffId'));
  if(startTariff>0)startData.tariffId=startTariff;
  const draft=add('sale_start',startData);
  if(!d||d.status==='AWAITING_RECEIPT'){const receipt=imageFiles(form,'receipt',8*1024*1024)[0];if(receipt)add('sale_receipt',{draft},receipt);else if(!data.bankConfigured&&Number(b.paidAmount||0)<=0)throw new Error('Прикрепите чек продажи или дождитесь подтверждения банка.');}
  if(!d||d.status!=='AWAITING_SELECTED'||total!==Number(d.total)||sold!==Number(d.sold))add('sale_counts',{draft,total,sold,price:b.price,tariffId:d?.pricing?.tariffId||Number(f.get('tariffId'))||null});
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
function revealBookingForm(form){
 form.closest('details').open=true;
 const target=form.elements.client_name;
 target.scrollIntoView({block:'center'});
 if(!target.disabled)target.focus();
}
async function resetCompletedForm(form,{refresh=true}={}){
 const keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=await outboxRows();
 if(!keys.length||!keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced')))throw new Error('Сначала завершите текущую отправку.');
 const id=form.id;
 clearForm(form);form.reset();form.remove();
 captureForms();if(refresh)await loadWorkflow();
 document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();
 return document.querySelector('#'+id);
}
export async function openWorkflowBooking(){
 let form=document.querySelector('#workflowBooking');
 if(!form)throw new Error('Создание записи недоступно. Проверьте рабочую роль и доступ к отелю.');
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=await outboxRows();
  if(keys.length&&keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced')))form=await resetCompletedForm(form,{refresh:false});
 }
 revealBookingForm(form);
 if(form.dataset.pendingKeys)throw new Error('Предыдущая запись ещё отправляется. Проверьте её результат и очередь ниже; повторно заполнять её не нужно.');
}
export async function workflowClick(button){
 const kind=button.dataset.workflow;
 if(kind==='receipt-view'){await viewReceipt(button.dataset.receiptPath);return;}
 if(kind==='sale-history'){await loadWorkflow(Number(button.dataset.booking));workflowSelect(button.dataset.booking);document.querySelector('#workflowSale')?.scrollIntoView({block:'center'});return;}
 if(kind==='repair-sale'){
  const form=button.closest('form');if(form?.id!=='workflowSale')return;
  await loadWorkflow(selected);const b=data.bookings.find(b=>b.id===selected),d=b?.draft,keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=keys.map(key=>queue.find(row=>row.key===key));
  const rejected=rejectedSaleCounts(rows);
  if(!d?.mine||!(rejected?['AWAITING_COUNTS','AWAITING_SELECTED'].includes(d.status):d.status==='AWAITING_SELECTED')||!saleQueueRepairable(rows))throw new Error('Сначала дождитесь подтверждения всех файлов или исправьте ошибку отправки в очереди.');
  form.dataset.repairing='1';form.elements.total.value=frameTotal(b)||d.total||'';form.elements.total.readOnly=Boolean(b.fullUploaded);form.elements.sold.value=(rejected?rejected.data.sold:d.sold)||'';form.elements.selected.value='';
  const summary=document.querySelector('#workflowSelectedSaleSummary');if(summary)summary.innerHTML=salesHistory(b)+draftProgress(b);
  updateFormProgress();form.elements[rejected?'total':'sold'].focus();return;
 }
 if(kind==='new-operation'){const form=button.closest('form');if(!form)return;const next=await resetCompletedForm(form,{refresh:form.id!=='workflowBooking'});if(next?.id==='workflowBooking')revealBookingForm(next);return;}

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
export function workflowSelect(id){captureForms();selected=Number(id)||null;try{sessionStorage.setItem('pb-workflow-sale:'+data.actorId,String(selected||''));}catch{}document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();refreshQuote();}
function refreshConnection(){const el=document.querySelector('#workflowNotice');if(el)el.textContent=connectionNote();}
window.addEventListener('online',refreshConnection);window.addEventListener('offline',refreshConnection);
let saleRefreshTimer=null,saleRefreshVersion=0,saleRefreshDue=0,lastSaleRefreshAt=0;
function scheduleSaleRefresh(immediate=false){
 const delay=immediate?350:Math.max(350,5000-(Date.now()-lastSaleRefreshAt)),due=Date.now()+delay;
 if(saleRefreshTimer&&saleRefreshDue<=due)return;
 clearTimeout(saleRefreshTimer);saleRefreshDue=due;
 const version=++saleRefreshVersion,actor=data?.actorId;
 saleRefreshTimer=setTimeout(async()=>{
  saleRefreshTimer=null;
  if(!document.querySelector('#workflowSalesSummary')||!actor||actor!==data?.actorId)return;
  try{
   lastSaleRefreshAt=Date.now();
   const booking=selected,next=await api('/workflow'+(booking?'?booking='+booking:''));
   if(version!==saleRefreshVersion||actor!==data?.actorId||next.actorId!==actor)return;
   if(booking!==selected){scheduleSaleRefresh();return;}
   data=next;
   const overview=document.querySelector('#workflowSalesSummary');if(overview)overview.innerHTML=salesOverview();
   const summary=document.querySelector('#workflowSelectedSaleSummary'),b=data.bookings.find(b=>b.id===selected);
   if(summary)summary.innerHTML=b?salesHistory(b)+draftProgress(b):'';
  }catch{ /* Keep local progress visible; an explicit refresh can retry the server. */ }
 },delay);
}
window.addEventListener('pb-outbox',async()=>{refreshConnection();const el=document.querySelector('#workflowQueue');if(el){
 const previous=new Map(queue.map(row=>[row.key,row.status]));queue=await outboxRows();
 el.innerHTML=queueView();updateFormProgress();
 const accepted=queue.filter(row=>row.kind.startsWith('sale_')&&row.status==='synced'&&previous.get(row.key)!=='synced');
 if(accepted.length)scheduleSaleRefresh(accepted.some(row=>row.kind==='sale_complete'));
}});

function updateFormProgress(){
 for(const form of document.querySelectorAll('#workflowBooking,#workflowSale,#workflowShoot')){
  if(!form.dataset.pendingKeys)continue;
  let keys;try{keys=JSON.parse(form.dataset.pendingKeys);}catch{continue;}
  const rows=keys.map(key=>queue.find(row=>row.key===key)),done=rows.filter(r=>r?.status==='synced').length;
  const last=rows.find(row=>row?.kind==='sale_complete')||rows[rows.length-1],finished=done===keys.length&&keys.length>0;
  let note=form.querySelector('[data-operation-progress]');if(!note){note=document.createElement('div');note.dataset.operationProgress='';note.setAttribute('role','status');form.append(note);}
  const failed=rows.find(r=>r?.error);
  const repairable=form.id==='workflowSale'&&saleQueueRepairable(rows),repairing=repairable&&form.dataset.repairing==='1',rejected=repairable&&rejectedSaleCounts(rows);
  note.textContent=repairing?(rejected?'Общее количество сверено с завершённой полной съёмкой. Сохраните исправление: прежний чек, проданное количество и выбранные файлы остаются в очереди и продолжат отправку.':'Исправьте фактическое количество проданных кадров или добавьте недостающие новые фотографии. Принятые чек и фото сохранятся. Повторные фотографии не увеличивают количество.'):finished?(form.id==='workflowBooking'&&rows[0]?.result?.bookingId?`Запись №${rows[0].result.bookingId} сохранена сервером.`:last?.result?.saleId?`Продажа №${last.result.saleId} сохранена сервером: ${last.result.soldPhotos??form.elements.sold?.value??'—'} фото на ${roubles(last.result.amount)}. Подтверждение оплаты показывается отдельно.`:'Операции приняты сервером. Для оригиналов отдельно проверьте статус на Яндекс.Диске.'):`В очереди сохранены все данные. Сервер принял ${done} из ${keys.length}. ${failed?.error||'Отправка продолжается.'} Повторное заполнение не требуется.`;
  if(form.id==='workflowSale'){
   let progress=form.querySelector('[data-sale-photo-progress]');
   if(!progress){progress=document.createElement('p');progress.dataset.salePhotoProgress='';progress.setAttribute('role','status');form.append(progress);}
   progress.textContent=saleTransferProgress(rows);
  }
  for(const el of [...form.elements])if(!['button','submit'].includes(el.type))el.disabled=true;
  if(repairing)for(const name of rejected?['total']:['total','sold','selected'])form.elements[name].disabled=false;
  const button=form.querySelector('button[type="submit"],button:not([type])');
  if(button){button.disabled=finished;button.textContent=repairing?(rejected?'Сохранить общее количество и продолжить':'Сохранить исправление и завершить'):finished?'Сохранено':failed?'Повторить отправку сохранённых данных':'Проверить отправку';}
  let repair=form.querySelector('[data-workflow="repair-sale"]');
  if(repairable&&!repair){repair=document.createElement('button');repair.type='button';repair.className='btn ghost';repair.dataset.workflow='repair-sale';repair.textContent='Исправить количество / добавить недостающие фото';form.append(repair);}
  if(repair)repair.textContent=rejected?'Сверить общее количество и продолжить отправку':'Исправить количество / добавить недостающие фото';
  if(repair)repair.hidden=!repairable||repairing;
  if(finished&&!form.querySelector('[data-workflow="new-operation"]')){const next=document.createElement('button');next.type='button';next.className='btn ghost';next.dataset.workflow='new-operation';next.textContent=form.id==='workflowBooking'?'Создать следующую запись':'Начать новую операцию';form.append(next);}
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

function refreshQuote(){const form=document.querySelector('#workflowSale'),b=data?.bookings.find(b=>b.id===selected);updatePricePreview(form,b,data?.tariffs);}
document.addEventListener('input',event=>{if(event.target.closest('#workflowSale'))refreshQuote();});
document.addEventListener('change',event=>{if(event.target.closest('#workflowSale'))refreshQuote();});
window.addEventListener('pb-workflow-mounted',refreshQuote);

window.addEventListener('pb-bank-paid',async()=>{await loadWorkflow(selected);refreshQuote();});
