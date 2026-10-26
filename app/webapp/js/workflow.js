import {saleDiskView,selectedDiskPhotos} from './sale-disk.js';
import {diskFolderPanel} from './disk-folder-audit.js';
import {bankPanel} from './bank-payments.js';
import {pricingView,tariffOptions,updatePricePreview,bookingPriceNote} from './hotel-pricing.js';
import {captureForm,captureForms,restoreForms,clearForm} from './form-state.js';
import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox,syncOutboxNow,enqueueOperation,enqueueFilesOneByOne,replaceOperationFile,canReplaceOperationFile,canReplaceDiskSelection,replaceDiskSelection,saleQueueRepairable,repairSaleQueue,rejectedSaleCounts,rejectedBooking,repairBookingQueue} from './outbox.js';
import {checkSaleFiles} from './sale-files.js';
let data=null,queue=[],selected=null,folderBusy=false,loadVersion=0,selectionVersion=0;
const discountRequests=new Set();
const discountRequestKey=(actor,booking)=>`${actor}:${booking}`;
const roubles=value=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2}).format(Number(value||0));
const frameTotal=b=>b.fullUploaded?(b.uploadedOriginals??b.totalFrames):(b.draft?.total||b.totalFrames||'');
const saleable=b=>Boolean(b.draft)||b.status==='READY_FOR_SALE'||(['SOLD','COMPLETED'].includes(b.status)&&b.sales?.length);
const workflowRoute=()=>typeof location==='undefined'?null:location.hash;
const selectionContext=()=>({version:selectionVersion,actor:data?.actorId,route:workflowRoute()});
const selectionCurrent=context=>context.version===selectionVersion&&context.actor===data?.actorId&&context.route===workflowRoute();
function mountDiscountPanel(){
 document.querySelector('#saleDiscountPanel')?.remove();
 const b=data?.bookings.find(row=>row.id===selected),form=document.querySelector('#workflowSale');
 if(!b||!form||b.sales?.length)return;
 const approvals=b.discountApprovals||[],fixed=b.draft?.pricing;
 const panel=document.createElement('section');panel.id='saleDiscountPanel';panel.className='notice';
 panel.innerHTML=`<h3>Вся съёмка со скидкой 50%</h3><p>Скидку подтверждает владелец или администратор отеля. Она действует только на все кадры съёмки; альбом оплачивается отдельно. Сначала укажите фактическое общее число кадров в форме продажи.</p><button type="button" class="btn ghost" data-sale-discount-request ${fixed?'disabled':''}>Запросить скидку 50% на всю съёмку</button>${approvals.map(row=>`<p>Запрос №${row.id}: ${row.total} кадров × ${esc(row.unitPrice)} ₽ × 50% = ${esc(row.amount)} ₽ · ${esc(({PENDING:'Ожидает подтверждения',APPROVED:'Подтверждён',REJECTED:'Отказано'})[row.status]||row.status)} ${data.canApproveDiscount&&row.status==='PENDING'?`<button type="button" class="btn" data-sale-discount-decision="${row.id}" data-decision="APPROVED">Подтвердить</button><button type="button" class="btn ghost" data-sale-discount-decision="${row.id}" data-decision="REJECTED">Отказать</button>`:''}</p>`).join('')}<label class="field">Скидка для этой продажи<select class="input" form="workflowSale" name="discountId" ${fixed?'disabled':''}><option value="">Без скидки</option>${approvals.filter(row=>row.status==='APPROVED').map(row=>`<option value="${row.id}" ${fixed?.discountId===row.id?'selected':''}>Согласована 50% · все ${row.total} кадров · ${esc(row.amount)} ₽</option>`).join('')}</select></label><p role="status"></p>`;
 form.before(panel);
 if(discountRequests.has(discountRequestKey(data.actorId,b.id)))for(const control of panel.querySelectorAll('button'))control.disabled=true;
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-sale-discount-request],[data-sale-discount-decision]');if(!button)return;
 const panel=button.closest('#saleDiscountPanel'),form=document.querySelector('#workflowSale'),b=data?.bookings.find(row=>row.id===selected),context=selectionContext();
 if(!b||!form||!panel)return;
 const requestKey=discountRequestKey(context.actor,b.id);
 if(discountRequests.has(requestKey))return;
 discountRequests.add(requestKey);
 const controls=[...panel.querySelectorAll('button')].map(control=>[control,control.disabled]);
 for(const [control] of controls)control.disabled=true;
 try{
  if(button.hasAttribute('data-sale-discount-request')){
   const total=Number(form?.elements.total.value);if(!Number.isInteger(total)||total<1||total>10000)throw Error('Укажите общее число кадров съёмки.');
   await api('/sale-discounts',{method:'POST',body:{bookingId:b.id,total}});
  }else await api(`/sale-discounts/${button.dataset.saleDiscountDecision}/decision`,{method:'POST',body:{decision:button.dataset.decision}});
  if(!selectionCurrent(context))return;
  captureForms();if(await loadWorkflow(b.id)){discountRequests.delete(requestKey);workflowSelect(b.id);}
 }catch(error){if(panel?.isConnected)panel.querySelector('[role=status]').textContent=error.message;}
 finally{
  discountRequests.delete(requestKey);
  if(panel.isConnected)for(const [control,disabled] of controls)control.disabled=disabled;
  else if(data?.actorId===context.actor&&selected===b.id&&workflowRoute()===context.route){
   const chosen=document.querySelector('#saleDiscountPanel [name=discountId]')?.value;
   mountDiscountPanel();
   const select=document.querySelector('#saleDiscountPanel [name=discountId]');
   if(select&&chosen&&[...select.options].some(option=>option.value===chosen))select.value=chosen;
  }
 }
});
const labels={local:'В очереди на этом устройстве — не отправлено',syncing:'Передаётся на сервер',synced:'Сервер принял',error:'Ошибка отправки'};
const kinds={booking:'Бронь',booking_receipt:'Чек брони',sale_start:'Черновик продажи',sale_receipt:'Чек продажи',sale_counts:'Количество кадров',sale_selected:'Выбранный кадр',sale_disk_selected:'Кадр с Яндекс.Диска',sale_complete:'Оформление продажи',shoot_photo:'Кадр полной съёмки',shoot_complete:'Завершение загрузки',checklist:'Контрольный пункт',shift_report:'Замечание',attendance:'Отметка смены'};
export async function loadWorkflow(bookingId=null){
 const ticket=++loadVersion,context=selectionContext(),suffix=bookingId||selected;let next,missing=false;
 const current=()=>ticket===loadVersion&&selectionCurrent(context);
 try{next=await api('/workflow'+(suffix?'?booking='+Number(suffix):''));}
 catch(error){if(!current())return false;if(!suffix||![403,404].includes(error.status))throw error;missing=true;next=await api('/workflow');}
 if(!current())return false;
 const rows=await outboxRows();
 if(!current())return false;
 if(missing||data?.actorId!==next.actorId)selected=null;
 data=next;queue=rows;
 if(bookingId&&data.bookings.some(b=>b.id===Number(bookingId)))selected=Number(bookingId);
 if(!selected)try{selected=Number(sessionStorage.getItem('pb-workflow-sale:'+data.actorId))||null;}catch{}
 if(selected&&!data.bookings.some(b=>b.id===selected))selected=null;
 return true;
}
function saleFormBooking(form,actor=data?.actorId){
 const id=Number(form.dataset.draftScope);
 if(form.isConnected===false||!Number.isSafeInteger(id)||id<1||id!==selected||actor!==data?.actorId)throw new Error('Съёмка или аккаунт изменились. Откройте нужную продажу и проверьте её данные.');
 const booking=data?.bookings.find(row=>row.id===id);
 if(!booking)throw new Error('Выберите доступную съёмку.');
 return booking;
}

function salesHistory(b){
 const rows=b.sales||[],receipts=b.receipts||[],draft=draftReceiptView(b.draft);if(!rows.length&&!receipts.length)return draft;
 return `<section class="notice" data-sales-history><h3>История покупок · ${esc(b.client||'Гость')}</h3>
 ${rows.map((sale,i)=>`<p>№${sale.id} · ${i?'Допродажа':'Первая покупка'} · ${esc(new Date(sale.at).toLocaleString('ru-RU'))} · ${sale.photos} фото${(sale.lines||[]).map(line=>` · ${line.type==='PACKAGE'?`пакет «${esc(line.name)}»`:line.type==='ALBUM'?`альбом «${esc(line.name)}» · ${line.quantity} шт. × ${esc(line.price)} ₽`:line.type==='DISCOUNT'?esc(line.name):`поштучно ${esc(line.price)} ₽ × ${line.quantity}`} = ${esc(roubles(line.amount))}`).join('')} · <strong>${esc(roubles(sale.amount))}</strong> · ${esc(({PAID:'Оплачено',PARTIAL:'Частично оплачено',UNPAID:'Ожидает оплаты'})[sale.paymentStatus]||sale.paymentStatus)}</p>`).join('')}
 <p><strong>Всего куплено: ${b.soldTotal} фото на ${esc(roubles(b.salesAmount))}.</strong><br>Подтверждено поступлений: ${esc(roubles(b.paidAmount))}. Остаток к оплате: ${esc(roubles(b.dueAmount))}.</p>${Number(b.creditAmount)>0?`<p>Неиспользованный остаток оплаты / к возврату после сверки: ${esc(roubles(b.creditAmount))}. Автоматического возврата нет; требуется решение руководителя.</p>`:''}
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

function replacementControl(r){if(r.kind==='sale_disk_selected'&&canReplaceDiskSelection(r))return `<button type="button" class="btn ghost" data-workflow="replace-disk-photo" data-key="${esc(r.key)}">Заменить отклонённый кадр выбранным на Диске</button>`;return canReplaceOperationFile(r)&&r.hasBlob?`<label class="field">${r.kind==='sale_receipt'?'Прикрепить другой чек к этой же продаже':'Исправить отклонённый файл без новой продажи'}<input class="input" type="file" accept="image/jpeg,image/png,image/webp" data-workflow-replace="${r.key}"></label>`:'';}
function queueView(){
 const inProgress=queue.filter(r=>r.status==='local'||r.status==='syncing').length;
 const errors=queue.filter(r=>r.status==='error').length;
 const stored=queue.filter(r=>r.status==='synced').length;
 const visible=[...queue.filter(r=>r.status!=='synced').slice(0,50),...queue.filter(r=>r.status==='synced').slice(-20)];
 const summary=`<p class="small">Ожидают отправки или отправляются: ${inProgress} · Ошибки: ${errors} · Принято сервером: ${stored}. Показаны первые 50 незавершённых и последние 20 завершённых операций.</p>`;
 return summary+(visible.map(r=>`<article class="panel"><strong>${r.kind==='shoot_photo'&&r.status==='synced'?'Сервер принял · проверьте Яндекс.Диск ниже':labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.photoId?` · кадр №${r.result.photoId}`:''}${r.filename?` · ${esc(r.filename)}`:''}${r.data?.shooting?.id?` · съёмка №${r.data.shooting.id}`:''}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.retryAt>Date.now()?`<p>Автоматический повтор не раньше ${esc(new Date(r.retryAt).toLocaleTimeString('ru-RU'))}. Остальные файлы отправляются независимо.</p>`:''}${r.status==='error'||(r.status==='local'&&r.error)?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">Повторить</button>`:''}${replacementControl(r)}</article>`).join('')||'<p>Операций в очереди нет.</p>');
}
function saleTransferProgress(rows){
 const photos=rows.filter(row=>['sale_selected','sale_disk_selected'].includes(row?.kind));if(!photos.length)return '';
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
 ${pricingView(data)}${diskFolderPanel(data)}${data.canBook?`<details class="panel section"><summary>Новая бронь</summary><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}<p id="bookingFixedPrice" class="notice">${esc(bookingPriceNote(data,data.hotels[0]?.id))}</p>${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Предоплата по записи, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="1000" required'))}<p>Обычно 1000 ₽; укажите другую сумму, если договорились иначе. Без подтверждения поступления эта сумма не уменьшает доплату.</p>${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа и допродажа</h2><form id="workflowLookup" class="row"><label>Номер записи <input class="input" name="bookingId" type="number" min="1" required></label><button class="btn ghost" type="submit">Найти прежнюю съёмку</button></form><p>Цена одного кадра закреплена за записью и не меняется при продаже. Можно выбрать пакет этого отеля. Сумма продажи, подтверждённая предоплата и остаток показываются отдельно.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(bookingTitle(x))}</option>`).join('')}</select><div id="workflowSelectedSaleSummary">${b?salesHistory(b)+draftProgress(b):''}</div>${b?(d&&!d.mine?`<p>Продажу оформляет ${esc(d.createdByName||'другой сотрудник')}. Сохранено выбранных кадров: ${d.selected||0} из ${d.sold||'—'}.</p>${data.canAuditDisk?`<p>Владелец может продолжить этот черновик. Чек, согласованная цена и принятые фотографии сохранятся; прежний сотрудник больше не сможет изменять эту продажу.</p><button type="button" class="btn primary" data-workflow="takeover-sale" data-booking="${b.id}">Продолжить продажу как владелец</button>`:''}`:`<form id="workflowSale" data-draft-scope="${b.id}" class="form-grid section"><p>${b.sales?.length?'Допродажа: укажите только новую покупку. ':''}Количество кадров можно указать любым целым числом в пределах фактически загруженной съёмки. Сервер проверит число и тариф повторно.</p>${!d||d.status==='AWAITING_RECEIPT'?field(b.sales?.length?'Новый чек доплаты (не прежний чек)':'Чек продажи',file('receipt',false,false)):''}${tariffOptions(b,data.tariffs)}${field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${frameTotal(b)}" ${b.fullUploaded?'readonly':''}`))}${field(b.sales?.length?'Дополнительно продано кадров':'Продано кадров',input('sold',`type="number" min="1" max="10000" required value="${d?.sold||''}"`))}<p>Уже сохранено выбранных кадров: ${d?.selected||0}. Прикрепите оставшиеся.${d?.status==='AWAITING_SELECTED'?' Если количество проданных кадров указано ошибочно, исправьте его выше: чек и принятые фото сохранятся. Уменьшить количество ниже уже принятых фото нельзя.':''} После смены Диска можно повторно выбрать все купленные кадры: дубликаты восстановятся без новой продажи.</p>${saleDiskView(b,data)}${field('Выбранные фотографии',file('selected',true,false))}<button class="btn primary">${b.sales?.length?'Добавить допродажу':'Сохранить продажу и фотографии'}</button></form>`):''}</section>
 ${bankPanel(b,data)}<section class="panel section" id="workflowSalesSummary">${salesOverview()}</section>
 <details class="panel section"><summary>Загрузить полную съёмку</summary>${upload.length?`<form id="workflowShoot" class="form-grid">${field('Съёмка',`<select class="input" name="booking">${options(upload)}</select>`)}${field('Фотографии',file('photos',true,false))}<label><input type="checkbox" name="finish"> Все кадры добавлены — завершить загрузку</label><p>Можно добавлять частями. Ставка фотографа фиксируется сервером после завершения полной загрузки.</p><button class="btn primary">Сохранить фотографии</button></form><label class="field section"><span>Выбрать папку — отправка сразу, по одному файлу</span><input id="workflowShootFolder" class="input" type="file" accept="image/jpeg,image/png,image/webp" webkitdirectory multiple></label><p id="workflowFolderProgress" class="notice" role="status">После выбора папки не закрывайте вкладку до конца передачи. Проверяйте Яндекс.Диск отдельно.</p>`:'<p>Нет доступных съёмок для полной загрузки.</p>'}</details>
 <section class="panel section"><h2>Файлы на Яндекс.Диске</h2><button class="btn ghost" data-workflow="refresh">Обновить состояние файлов</button>${data.bookings.filter(b=>b.shootingId).map(b=>`<details data-storage-shooting="${b.shootingId}"><summary>${esc(bookingTitle(b))} · сохранено ${(b.files||[]).filter(f=>f.status==='STORED').length} из ${(b.files||[]).length}</summary>${(b.files||[]).map(f=>`<p>Кадр №${f.id}: ${esc(({STORED:'Сохранено на Диске',PENDING:'Ожидает сохранения',UPLOADING:'Сохраняется',FAILED:'Ошибка сохранения'})[f.status])} ${esc(f.error||'')}</p>`).join('')}${editView(b)}${b.canUpload?`<button class="btn ghost" data-workflow="retry-storage" data-shooting="${b.shootingId}">Восстановить сохранение на Диске</button>`:''}</details>`).join('')||'<p>Съёмок пока нет.</p>'}</section>
 <section class="section"><h2>Синхронизация</h2><button class="btn ghost" data-workflow="sync">Синхронизировать сейчас</button><div id="workflowSyncNote" class="notice" role="status">Отправка запускается автоматически при наличии сети. Нажмите кнопку для ручной проверки.</div><div id="workflowQueue">${queueView()}</div></section>`;
}
function imageFiles(form,name,maximum=20*1024*1024){const list=[...form.elements[name].files];for(const f of list)if(!(['image/jpeg','image/png','image/webp'].includes(f.type)||(!f.type&&/\.(jpe?g|png|webp)$/i.test(f.name)))||!f.size||f.size>maximum)throw new Error(`Изображения JPEG, PNG или WebP, не больше ${maximum/1024/1024} МБ.`);return list;}
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
 const actor=data?.actorId;if(form.id==='workflowSale')saleFormBooking(form,actor);
 captureForm(form);
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=(await outboxRows()).filter(r=>keys.includes(r.key));
  if(rows.length!==keys.length)throw new Error('Часть очереди не найдена. Не создавайте повторную продажу; проверьте журнал синхронизации.');
  if(rows.every(r=>r.status==='synced'))throw new Error('Эта операция уже сохранена. Для новой используйте кнопку под результатом.');
  if(form.id==='workflowBooking'&&form.dataset.repairingBooking==='1'){
   await repairBookingQueue(keys,bookingFormData(form));delete form.dataset.repairingBooking;captureForm(form);
   queue=await outboxRows();updateFormProgress();return;
  }
  if(form.id==='workflowSale'&&form.dataset.repairing==='1'){
   const b=saleFormBooking(form,actor),d=b?.draft,total=Number(form.elements.total.value),sold=Number(form.elements.sold.value),photos=imageFiles(form,'selected');
   const rejected=rejectedSaleCounts(rows),diskPhotos=!rejected&&(form.elements.photoSource?.value||'device')!=='device'?selectedDiskPhotos(form):[];
   if(!d?.mine||!(rejected?['AWAITING_COUNTS','AWAITING_SELECTED'].includes(d.status):d.status==='AWAITING_SELECTED')||!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте фактическое количество кадров и доступ к продаже.');
   if(sold<d.selected)throw new Error(`Уже принято ${d.selected} выбранных фото. Проданное количество не может быть меньше.`);
   if(!rejected&&(photos.length+diskPhotos.length>sold||photos.length+diskPhotos.length+Number(d.selected||0)<sold))throw new Error(`Не хватает ${Math.max(0,sold-Number(d.selected||0))} новых выбранных кадров. Повторные фотографии не увеличивают количество.`);
   await checkSaleFiles(photos);saleFormBooking(form,actor);
   const added=await repairSaleQueue(keys,{draftId:d.id,total,sold,price:b.price},photos,diskPhotos);
   form.dataset.pendingKeys=JSON.stringify([...keys,...added.map(row=>row.key)]);delete form.dataset.repairing;captureForm(form);
   queue=await outboxRows();updateFormProgress();return;
  }
  for(const row of rows.filter(r=>r.status==='error'||r.error))await retryOperation(row.key);
  await flushOutbox();queue=await outboxRows();updateFormProgress();return;
 }
 const f=new FormData(form),date=todayKey(),items=[],add=(kind,data,blob=null)=>{const row={key:crypto.randomUUID(),kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.classList.contains('photo-edit')){add('photo_edit',{shooting:Number(form.dataset.shooting),photo:Number(form.dataset.photo),action:'MANUAL',edit:null,parameters:Object.fromEntries(['exposure','contrast','color','red','blue'].map(k=>[k,Number(f.get(k))]))});
 }else if(form.id==='workflowBooking'){
  const receipt=imageFiles(form,'receipt',8*1024*1024)[0];
  const booking=add('booking',bookingFormData(form));
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
 }else if(form.id==='workflowSale'){
  const b=saleFormBooking(form,actor);const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const useDisk=(form.elements.photoSource?.value||'device')!=='device',diskPhotos=useDisk?selectedDiskPhotos(form):[],photos=useDisk?[]:imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте количество кадров.');
  if(sold<(d?.selected||0))throw new Error(`Уже принято ${d.selected} выбранных фото. Проданное количество не может быть меньше.`);
  if(photos.length+diskPhotos.length>sold||photos.length+diskPhotos.length+(d?.selected||0)<sold)throw new Error(`Нужно прикрепить ${Math.max(0,sold-(d?.selected||0))} новых выбранных кадров. Повторные фотографии не увеличивают количество.`);
  await checkSaleFiles(photos);saleFormBooking(form,actor);
  const startData={booking:{id:b.id},price:b.price},startTariff=d?.pricing?.tariffId||Number(f.get('tariffId'));
  if(startTariff>0)startData.tariffId=startTariff;
  const draft=add('sale_start',startData);
  if(!d||d.status==='AWAITING_RECEIPT'){const receipt=imageFiles(form,'receipt',8*1024*1024)[0];if(receipt)add('sale_receipt',{draft},receipt);else if(!data.bankConfigured&&Number(b.paidAmount||0)<=0)throw new Error('Прикрепите чек продажи или дождитесь подтверждения банка.');}
  if(!d||d.status!=='AWAITING_SELECTED'||total!==Number(d.total)||sold!==Number(d.sold))add('sale_counts',{draft,total,sold,price:b.price,tariffId:d?.pricing?.tariffId||Number(f.get('tariffId'))||null,discountId:d?.pricing?.discountId||Number(f.get('discountId'))||null,albumId:Number(f.get('albumId'))||null,albumQuantity:f.get('albumId')?Number(f.get('albumQuantity')||1):null});
  for(const photo of photos)add('sale_selected',{draft},photo);
  for(const photo of diskPhotos)add('sale_disk_selected',{draft,...photo});
  add('sale_complete',{draft,price:b.price});
 }else{
  const b=data.bookings.find(b=>b.id===Number(f.get('booking')));if(!b)throw new Error('Выберите съёмку.');
  for(const photo of imageFiles(form,'photos'))add('shoot_photo',{shooting:{id:b.shootingId}},photo);
  if(f.get('finish'))add('shoot_complete',{shooting:{id:b.shootingId}});
 }
 if(form.id==='workflowSale')saleFormBooking(form,actor);
 await enqueueBatch(items);
 form.dataset.pendingKeys=JSON.stringify(items.map(row=>row.key));captureForm(form);
 queue=await outboxRows();const list=document.querySelector('#workflowQueue');if(list)list.innerHTML=queueView();updateFormProgress();
}
function bookingFormData(form){
 const f=new FormData(form);
 return {client_name:f.get('client_name'),client_phone:f.get('client_phone')?.trim()||null,hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit:f.get('deposit'),shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null};
}
function revealBookingForm(form){
 form.closest('details').open=true;
 const target=form.elements.client_name;
 target.scrollIntoView({block:'center'});
 if(!target.disabled)target.focus();
}
async function resetCompletedForm(form,{refresh=true}={}){
 if(form.dataset.resetting==='1')return null;
 form.dataset.resetting='1';form.setAttribute('aria-busy','true');
 const picker=form.id==='workflowSale'?document.querySelector('#workflowSalePick'):null;
 const controls=[...form.elements,...(picker?[picker]:[])].map(el=>[el,el.disabled]);
 for(const [el] of controls)el.disabled=true;
 try{
  const keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=await outboxRows();
  const savedBooking=form.id==='workflowBooking'&&bookingAccepted(keys.map(k=>rows.find(r=>r.key===k)));
  if(!keys.length||(!savedBooking&&!keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced'))))throw new Error('Сначала завершите текущую отправку.');
  const id=form.id;
  if(refresh&&!await loadWorkflow())return null;
  if(!form.isConnected)return null;
  clearForm(form);form.reset();form.remove();
  captureForms();
  document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();
  return document.querySelector('#'+id);
 }finally{
  for(const [el,disabled] of controls)if(el.isConnected)el.disabled=disabled;
  if(form.isConnected){delete form.dataset.resetting;form.removeAttribute('aria-busy');}
 }
}
export async function openWorkflowBooking(){
 let form=document.querySelector('#workflowBooking');
 if(!form)throw new Error('Создание записи недоступно. Проверьте рабочую роль и доступ к отелю.');
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=await outboxRows();
  if(keys.length&&(bookingAccepted(keys.map(k=>rows.find(r=>r.key===k)))||keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced'))))form=await resetCompletedForm(form,{refresh:false});
  else if(rejectedBooking(keys.map(k=>rows.find(r=>r.key===k)))){form.dataset.repairingBooking='1';queue=rows;updateFormProgress();}
 }
 if(!form)return;
 revealBookingForm(form);
 if(form.dataset.pendingKeys&&form.dataset.repairingBooking!=='1')throw new Error('Предыдущая запись ещё отправляется. Проверьте её результат и очередь ниже; повторно заполнять её не нужно.');
}
export async function workflowClick(button){
 const kind=button.dataset.workflow;
 if(kind==='replace-disk-photo'){
  const form=document.querySelector('#workflowSale');if(!form)throw new Error('Откройте продажу с отклонённым кадром.');
  const actor=data?.actorId,b=saleFormBooking(form,actor),key=button.dataset.key,keys=JSON.parse(form.dataset.pendingKeys||'[]');
  if(!keys.includes(key))throw new Error('Откройте исходную продажу этого кадра. Другая съёмка не будет изменена.');
  const photos=selectedDiskPhotos(form);if(photos.length!==1)throw new Error('Выберите на Диске ровно один новый кадр для замены отклонённого.');
  const rows=await outboxRows();saleFormBooking(form,actor);
  if(!canReplaceDiskSelection(rows.find(row=>row.key===key)))throw new Error('Замена недоступна: проверьте результат прежней отправки.');
  await replaceDiskSelection(key,photos[0],b.draft?.id);queue=await outboxRows();updateFormProgress();return;
 }
 if(kind==='takeover-sale'){const booking=Number(button.dataset.booking),context=selectionContext();await api(`/bookings/${booking}/sale-takeover`,{method:'POST',body:{}});if(!selectionCurrent(context))return;captureForms();if(!await loadWorkflow(booking))return;document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();refreshQuote();return;}
 if(kind==='receipt-view'){await viewReceipt(button.dataset.receiptPath);return;}
 if(kind==='sale-history'){if(!await loadWorkflow(Number(button.dataset.booking)))return;workflowSelect(button.dataset.booking);document.querySelector('#workflowSale')?.scrollIntoView({block:'center'});return;}
 if(kind==='repair-booking'){
  const form=button.closest('form');if(form?.id!=='workflowBooking')return;
  queue=await outboxRows();const keys=JSON.parse(form.dataset.pendingKeys||'[]');
  if(!rejectedBooking(keys.map(key=>queue.find(row=>row.key===key))))throw new Error('Исправление недоступно: сначала проверьте результат прежней отправки.');
  form.dataset.repairingBooking='1';updateFormProgress();revealBookingForm(form);return;
 }
 if(kind==='repair-sale'){
  const form=button.closest('form');if(form?.id!=='workflowSale')return;
  const booking=saleFormBooking(form).id;if(!await loadWorkflow(booking)||form.isConnected===false)return;const b=saleFormBooking(form),d=b?.draft,keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=keys.map(key=>queue.find(row=>row.key===key));
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
if(button.dataset.workflow==='refresh'){captureForms();if(!await loadWorkflow())return;document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();return;}if(button.dataset.workflow==='retry-storage'){await enqueueOperation('shoot_retry',todayKey(),{shooting:{id:Number(button.dataset.shooting)}});return;}if(button.dataset.workflow==='retry')await retryOperation(button.dataset.key);else await flushOutbox();}
export function workflowSelect(id){if(!data||document.querySelector('#workflowSale')?.dataset.resetting==='1')return;selectionVersion++;loadVersion++;captureForms();selected=Number(id)||null;try{sessionStorage.setItem('pb-workflow-sale:'+data.actorId,String(selected||''));}catch{}document.querySelector('#app').innerHTML=workflowView();mountDiscountPanel();restoreForms();updateFormProgress();refreshQuote();}
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
   const booking=selected,context=selectionContext(),load=loadVersion,next=await api('/workflow'+(booking?'?booking='+booking:''));
   if(version!==saleRefreshVersion||actor!==data?.actorId||next.actorId!==actor)return;
   if(!selectionCurrent(context)||load!==loadVersion)return;
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

export function bookingAccepted(rows){return Boolean(rows[0]?.kind==='booking'&&rows[0]?.status==='synced'&&Number.isSafeInteger(rows[0]?.result?.bookingId)&&rows[0].result.bookingId>0);}
function updateFormProgress(){
 for(const form of document.querySelectorAll('#workflowBooking,#workflowSale,#workflowShoot')){
  if(!form.dataset.pendingKeys)continue;
  let keys;try{keys=JSON.parse(form.dataset.pendingKeys);}catch{continue;}
  const rows=keys.map(key=>queue.find(row=>row.key===key)),done=rows.filter(r=>r?.status==='synced').length;
  const last=rows.find(row=>row?.kind==='sale_complete')||rows[rows.length-1],allFinished=done===keys.length&&keys.length>0,finished=allFinished||(form.id==='workflowBooking'&&bookingAccepted(rows));
  let note=form.querySelector('[data-operation-progress]');if(!note){note=document.createElement('div');note.dataset.operationProgress='';note.setAttribute('role','status');form.append(note);}
  const failed=rows.find(r=>r?.error);
  const repairable=form.id==='workflowSale'&&saleQueueRepairable(rows),repairing=repairable&&form.dataset.repairing==='1',rejected=repairable&&rejectedSaleCounts(rows);
  const bookingRepairable=form.id==='workflowBooking'&&rejectedBooking(rows),bookingRepairing=bookingRepairable&&form.dataset.repairingBooking==='1';
  note.textContent=bookingRepairing?`Запись не была создана: ${bookingRepairable.error} Исправьте поля и сохраните повторно. Прежний чек остаётся в очереди.`:bookingRepairable?`Запись не была создана: ${bookingRepairable.error} Нажмите «Исправить запись», чтобы изменить данные этой же отправки.`:repairing?(rejected?'Общее количество сверено с завершённой полной съёмкой. Сохраните исправление: прежний чек, проданное количество и выбранные файлы остаются в очереди и продолжат отправку.':'Исправьте фактическое количество проданных кадров или добавьте недостающие новые фотографии. Принятые чек и фото сохранятся. Повторные фотографии не увеличивают количество.'):finished?(form.id==='workflowBooking'&&rows[0]?.result?.bookingId?`Запись №${rows[0].result.bookingId} сохранена сервером.`:last?.result?.saleId?`Продажа №${last.result.saleId} сохранена сервером: ${last.result.soldPhotos??form.elements.sold?.value??'—'} фото на ${roubles(last.result.amount)}. Подтверждение оплаты показывается отдельно.`:'Операции приняты сервером. Для оригиналов отдельно проверьте статус на Яндекс.Диске.'):`В очереди сохранены все данные. Сервер принял ${done} из ${keys.length}. ${failed?.error||'Отправка продолжается.'} Повторное заполнение не требуется.`;
  if(finished&&!allFinished&&form.id==='workflowBooking')note.textContent+=' Чек предоплаты ещё не принят. Он остаётся в очереди ниже; можно создать следующую запись.';
  if(form.id==='workflowSale'){
   let progress=form.querySelector('[data-sale-photo-progress]');
   if(!progress){progress=document.createElement('p');progress.dataset.salePhotoProgress='';progress.setAttribute('role','status');form.append(progress);}
   progress.textContent=saleTransferProgress(rows);
  }
  for(const el of [...form.elements])if(!['button','submit'].includes(el.type))el.disabled=true;
  if(bookingRepairing)for(const el of [...form.elements])if(el.name&&el.type!=='file')el.disabled=false;
  if(repairing)for(const name of rejected?['total']:['total','sold','selected'])form.elements[name].disabled=false;
  if(repairing&&!rejected)for(const el of form.querySelectorAll('[data-sale-disk] input,[data-sale-disk] button,[data-sale-disk] fieldset'))el.disabled=false;
  if(form.id==='workflowSale'&&rows.some(row=>row?.kind==='sale_disk_selected'&&canReplaceDiskSelection(row))){
   for(const el of form.querySelectorAll('[data-sale-disk] input,[data-sale-disk] button,[data-sale-disk] fieldset'))el.disabled=false;
   note.textContent+=' Папка Диска изменилась: выберите один новый кадр в форме продажи и нажмите «Заменить отклонённый кадр» у ошибки в очереди. Чек и принятые фото сохранятся.';
  }
  const button=form.querySelector('button[type="submit"],button:not([type])');
  if(button){button.disabled=finished;button.textContent=bookingRepairing?'Сохранить исправленную запись':repairing?(rejected?'Сохранить общее количество и продолжить':'Сохранить исправление и завершить'):finished?'Сохранено':failed?'Повторить отправку сохранённых данных':'Проверить отправку';}
  let bookingRepair=form.querySelector('[data-workflow="repair-booking"]');
  if(bookingRepairable&&!bookingRepair){bookingRepair=document.createElement('button');bookingRepair.type='button';bookingRepair.className='btn ghost';bookingRepair.dataset.workflow='repair-booking';bookingRepair.textContent='Исправить запись';form.append(bookingRepair);}
  if(bookingRepair)bookingRepair.hidden=!bookingRepairable||bookingRepairing;
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
 try{const id=Number(new FormData(form).get('bookingId'));captureForms();if(!await loadWorkflow(id))return;if(!data.bookings.some(b=>b.id===id))throw Error('Съёмка не найдена или недоступна.');workflowSelect(id);}
 catch(error){const el=document.querySelector('#workflowNotice');if(el)el.textContent=error.message;}finally{button.disabled=false;}
});

function refreshQuote(){const form=document.querySelector('#workflowSale'),b=data?.bookings.find(b=>b.id===selected);updatePricePreview(form,b,data?.tariffs);}
document.addEventListener('input',event=>{if(event.target.closest('#workflowSale'))refreshQuote();});
document.addEventListener('change',event=>{if(event.target.closest('#workflowSale')||event.target.name==='discountId')refreshQuote();});
window.addEventListener('pb-workflow-mounted',()=>{mountDiscountPanel();refreshQuote();});

window.addEventListener('pb-bank-paid',async event=>{if(event.detail?.bookingId&&Number(event.detail.bookingId)!==selected)return;if(await loadWorkflow(selected))refreshQuote();});

document.addEventListener('change',event=>{if(event.target?.name==='hotel_id'&&event.target.form?.id==='workflowBooking'){const note=document.querySelector('#bookingFixedPrice');if(note)note.textContent=bookingPriceNote(data,Number(event.target.value));}});

function invalidateWorkflowView(){loadVersion++;selectionVersion++;saleRefreshVersion++;if(saleRefreshTimer)clearTimeout(saleRefreshTimer);saleRefreshTimer=null;}
window.addEventListener('hashchange',invalidateWorkflowView);
window.addEventListener('pagehide',invalidateWorkflowView);
function resetWorkflow(){invalidateWorkflowView();data=null;queue=[];selected=null;clearComparisons();clearReceiptViewer();}
document.addEventListener('pb-chat-account-reset',resetWorkflow);
window.addEventListener('pb-operations-reset',resetWorkflow);
