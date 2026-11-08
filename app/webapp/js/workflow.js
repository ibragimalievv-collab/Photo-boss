import {saleDiskView,selectedDiskPhotos} from './sale-disk.js';
import {guestPhone,phoneMatches} from './guest-phone.js';
import {gallerySaleView,selectedGalleryPhotos,openGallerySale,galleryRecoveryKeys} from './gallery-sale.js';
import {diskFolderPanel} from './disk-folder-audit.js';
import {bankPanel,bankConnectionPanel,bankPaymentPayload,bankPaymentLink,bankPaymentStatus,openBankPaymentLink,showBookingBankPayment} from './bank-payments.js';
import {showMerchantQR} from './merchant-qr.js';
import {openReceiptReview,receiptReviewButton} from './receipt-review.js';
import {receiptTitle,receiptMessage} from './receipt-state.js';
import {receiptFiles} from './receipt-files.js';
import {pricingView,tariffOptions,updatePricePreview,bookingPriceNote} from './hotel-pricing.js';
import {captureForm,captureForms,restoreForms,clearForm} from './form-state.js';
import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox,syncOutboxNow,enqueueOperation,enqueueFilesOneByOne,replaceOperationFile,canReplaceOperationFile,canReplaceDiskSelection,replaceDiskSelection,saleQueueRepairable,repairSaleQueue,recoverSaleFromGallery,rejectedSaleCounts,rejectedBooking,repairBookingQueue,rejectedInlineSale,repairInlineSaleQueue} from './outbox.js';
import {checkSaleFiles,saleSaveState} from './sale-files.js';
let data=null,queue=[],selected=null,folderBusy=false,loadVersion=0,selectionVersion=0,queueRefreshVersion=0,bookingCorrection=null;
let quickBooking=null;
const receiptsOnly=()=>window.PHOTO_BOSS_CONFIG?.PAYMENT_MODE==='receipts';
const paymentMode=value=>receiptsOnly()?{...value,paymentMode:'receipts',bankConfigured:false,merchantQRConfigured:false,canCheckBankConnection:false}:value;
const submittingForms=new WeakMap(),saleQRs=new Map(),merchantPreparations=new WeakMap();
const isInline=value=>typeof value==='string'&&value.startsWith('inline:');
const isSaleStart=row=>['sale_start','sale_start_inline'].includes(row?.kind);
function currentBooking(form=null){
 if(!isInline(selected))return data?.bookings.find(row=>row.id===selected);
 const start=queue.find(row=>row.kind==='sale_start_inline'&&'inline:'+row.key===selected),saved=data?.bookings.find(row=>row.id===start?.result?.bookingId);
 const guest=start?.data?.guest||{},fields=form?.elements,config=data?.inlineSale||{};
 const hotelId=Number(fields?.hotel_id?.value||guest.hotelId||config.defaultHotelId)||null,hotel=data?.hotels.find(row=>row.id===hotelId);
 const packageId=Number(fields?.package_id?.value||guest.packageId)||null,pkg=data?.packages.find(row=>row.id===packageId);
 const price=hotel?.unitPrice??(pkg?.price||(data?.packages?.length&&new Set(data.packages.map(row=>String(row.price))).size===1?data.packages[0].price:null));
 return {...(saved||{}),id:saved?.id||start?.result?.bookingId||selected,inline:true,inlineGuest:guest,scope:selected,client:fields?.guest_name?.value||guest.name||'Новый гость',hotelId,hotel:hotel?.name,price:price==null?null:String(price),date:guest.date||data?.date||todayKey(),time:guest.time||config.time,soldTotal:saved?.soldTotal||0,paidAmount:saved?.paidAmount||0,pricing:price==null?null:{kind:'UNIT',price:String(price),tariffId:hotel?.fixedUnitTariffId||null},draft:saved?.draft||null};
}
function saleSelectionOptions(){
 const pending=queue.filter(row=>row.kind==='sale_start_inline'&&queue.some(step=>(step.key===row.key||step.data?.draft?.key===row.key)&&step.status!=='synced'));
 const scopes=new Map(pending.map(row=>['inline:'+row.key,row.data?.guest?.name||'Новый гость']));
 if(isInline(selected)&&!scopes.has(selected))scopes.set(selected,'Новый гость');
 return [...scopes].map(([scope,name])=>`<option value="${scope}" ${scope===selected?'selected':''}>Продолжить · ${esc(name)}</option>`).join('');
}
const roubles=value=>new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:2}).format(Number(value||0));
const frameTotal=b=>b.fullUploaded?(b.uploadedOriginals??b.totalFrames):(b.draft?.total||b.totalFrames||'');
const saleable=b=>Boolean(b.draft)||b.status==='READY_FOR_SALE'||(['SOLD','COMPLETED'].includes(b.status)&&b.sales?.length);
const workflowRoute=()=>typeof location==='undefined'?null:location.hash;
const selectionContext=()=>({version:selectionVersion,actor:data?.actorId,route:workflowRoute()});
const selectionCurrent=context=>context.version===selectionVersion&&context.actor===data?.actorId&&context.route===workflowRoute();
function mountDiscountPanel(){document.querySelector('#saleDiscountPanel')?.remove();}
const labels={local:'В очереди на этом устройстве — не отправлено',syncing:'Передаётся на сервер',synced:'Сервер принял',error:'Ошибка отправки'};
const kinds={booking:'Бронь',booking_bank_payment:'QR предоплаты',sale_bank_payment:'QR оплаты продажи',booking_receipt:'Чек брони',sale_start:'Черновик продажи',sale_start_inline:'Продажа нового гостя',sale_receipt:'Чек продажи',sale_counts:'Количество кадров',sale_selected:'Выбранный кадр',sale_disk_selected:'Кадр с Яндекс.Диска',sale_gallery_selected:'Кадр из галереи съёмки',sale_complete:'Оформление продажи',shoot_photo:'Кадр полной съёмки',shoot_complete:'Завершение загрузки',checklist:'Контрольный пункт',shift_report:'Замечание',attendance:'Отметка смены'};
export async function loadWorkflow(bookingId=null,keepSelection=false){
 const ticket=++loadVersion,context=selectionContext(),chosen=bookingId||selected,suffix=Number(chosen)>0?Number(chosen):null;let next,missing=false;
 const current=()=>ticket===loadVersion&&selectionCurrent(context);
 try{next=await api('/workflow'+(suffix?'?booking='+Number(suffix):''));}
 catch(error){if(!current())return false;if(!suffix||![403,404].includes(error.status))throw error;missing=true;next=await api('/workflow');}
 if(!current())return false;
 const rows=await outboxRows();
 if(!current())return false;
 if(missing||data?.actorId!==next.actorId)selected=null;
 data=paymentMode(next);queue=rows;
 if(bookingId&&!keepSelection&&data.bookings.some(b=>b.id===Number(bookingId)))selected=Number(bookingId);
 if(!selected)try{const saved=sessionStorage.getItem('pb-workflow-sale:'+data.actorId);selected=isInline(saved)?saved:Number(saved)||null;}catch{}
 if(selected&&!isInline(selected)&&!data.bookings.some(b=>b.id===selected))selected=null;
 return true;
}
function saleFormBooking(form,actor=data?.actorId){
 const scope=form.dataset.draftScope,id=isInline(scope)?scope:Number(scope);
 if(form.isConnected===false||id!==selected||actor!==data?.actorId)throw new Error('Съёмка или аккаунт изменились. Откройте нужную продажу и проверьте её данные.');
 const booking=currentBooking(form);
 if(!booking)throw new Error('Выберите доступную съёмку.');
 return booking;
}

function salesHistory(b){
 const rows=b.sales||[],receipts=b.receipts||[],draft=draftReceiptView(b.draft);if(!rows.length&&!receipts.length)return draft;
 return `<section class="notice" data-sales-history><h3>История покупок · ${esc(b.client||'Гость')}</h3>
 ${rows.map((sale,i)=>`<p>№${sale.id} · ${i?'Допродажа':'Первая покупка'} · ${esc(new Date(sale.at).toLocaleString('ru-RU'))} · ${sale.photos} фото${(sale.lines||[]).map(line=>` · ${line.type==='PACKAGE'?`пакет «${esc(line.name)}»`:line.type==='ALBUM'?`альбом «${esc(line.name)}» · ${line.quantity} шт. × ${esc(line.price)} ₽`:line.type==='DISCOUNT'?esc(line.name):`поштучно ${esc(line.price)} ₽ × ${line.quantity}`} = ${esc(roubles(line.amount))}`).join('')} · <strong>${esc(roubles(sale.amount))}</strong> · ${esc(({PAID:'Оплачено',PARTIAL:'Частично оплачено',UNPAID:'Ожидает оплаты'})[sale.paymentStatus]||sale.paymentStatus)}</p>`).join('')}
 <p><strong>Всего куплено: ${b.soldTotal} фото на ${esc(roubles(b.salesAmount))}.</strong><br>Учтено оплат: ${esc(roubles(b.paidAmount))}. Остаток к оплате: ${esc(roubles(b.dueAmount))}.</p>${Number(b.creditAmount)>0?`<p>Неиспользованный остаток оплаты / к возврату после сверки: ${esc(roubles(b.creditAmount))}. Автоматического возврата нет; требуется решение руководителя.</p>`:''}
 <p>Предыдущие продажи и чеки не перезаписываются. Ниже укажите только дополнительные фотографии и новый чек.</p>
 ${receipts.map(receipt=>`<article data-receipt-status="${receipt.id}"><strong>Чек №${receipt.id}: ${esc(receiptTitle(receipt))}</strong>${(receipt.images?.length?receipt.images:receipt.imagePath?[{path:receipt.imagePath}]:[]).map((file,i)=>`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(file.path)}">Посмотреть чек · файл ${i+1}</button>`).join('')}<p>${esc(receiptAnalysisLabel(receipt.analysisStatus))}${receipt.extractedAmount!=null?` · на чеке: ${esc(roubles(receipt.extractedAmount))}`:''}${receipt.expectedAmount!=null?` · ожидается: ${esc(roubles(receipt.expectedAmount))}`:''}</p>${(receipt.warnings||[]).map(w=>`<p>${esc(w)}</p>`).join('')}<p>${esc(receiptMessage(receipt,{automatic:data.receiptAutomatic}))}</p>${receipt.automatic?.state==='retry_exhausted'?`<button class="btn" data-workflow="receipt-retry" data-receipt="${receipt.id}">Повторить AI-проверку</button>`:''}${!data.receiptAutomatic?receiptReviewButton(data.canAuditDisk?['OWNER']:[],receipt.id):''}</article>`).join('')}${draft}</section>`;
}
function receiptAnalysisLabel(status){return ({READY:'Данные чека распознаны AI; это не подтверждение поступления денег.',SUCCESS:'Данные чека распознаны AI; это не подтверждение поступления денег.',EXTRACTED:'Данные чека распознаны AI; это не подтверждение поступления денег.',UNAVAILABLE:'AI пока не прочитал чек. Состояние проверки указано ниже.',FAILED:'AI пока не прочитал чек. Состояние проверки указано ниже.',PENDING:'AI-проверка ожидается.'})[String(status||'').toUpperCase()]||'Результат AI-проверки пока отсутствует.';}
function draftReceiptView(d){if(!d)return '';const automatic=data?.receiptAutomatic===true;return `<section data-draft-receipt><p>${d.receiptStored?'Файл чека принят сервером. '+esc(receiptAnalysisLabel(d.receiptAnalysisStatus)):'Файл чека ещё не принят сервером — проверьте очередь отправки ниже.'} ${automatic?'Проверка чека и зачёт доплаты выполнятся автоматически при завершении продажи. Решение владельца не требуется.':'Подтвердить оплату может только владелец после сверки поступления.'}</p>${(d.receiptImages?.length?d.receiptImages:d.receiptImagePath?[{path:d.receiptImagePath}]:[]).map((file,i)=>`<button class="btn ghost" data-workflow="receipt-view" data-receipt-path="${esc(file.path)}">Посмотреть чек · файл ${i+1}</button>`).join('')}</section>`;}
function draftProgress(b){
 const d=b.draft;if(!d)return '';
 const sold=Number(d.sold||0),saved=Number(d.selected||0),remaining=Math.max(0,sold-saved);
 const next=d.status==='AWAITING_RECEIPT'?'Прикрепите чек продажи.':d.status==='AWAITING_COUNTS'?'Укажите общее и проданное количество кадров.':saved>sold?`Принято ${saved} фото при указанном количестве ${sold}. Проверьте и исправьте количество проданных кадров; файлы сохранены.`:remaining?`Добавьте недостающие выбранные фотографии: ${remaining}.`:'Все выбранные фотографии приняты. Завершите продажу.';
 return `<section class="notice" data-sale-draft="${d.id}"><strong>Оформление продажи не завершено</strong><p>Всего кадров: ${frameTotal(b)||'не указано'} · Продано: ${sold||'не указано'} · Выбранных фото принято: ${saved} из ${sold||'—'}.</p>${d.amount!=null?`<p>Сумма продажи: <strong>${esc(roubles(d.amount))}</strong>${d.expectedAmount!=null?` · Ожидаемая доплата: ${esc(roubles(d.expectedAmount))}`:''}.</p>`:''}<p>${esc(next)}</p>${!d.mine?`<p>Оформляет: ${esc(d.createdByName||'другой сотрудник')}. ${data.canAuditDisk?'Владелец может продолжить продажу кнопкой ниже.':'Продолжить может сотрудник, начавший продажу.'}</p>`:''}</section>`;
}
function salesOverview(){
 const bookings=data.bookings.filter(b=>b.draft||b.sales?.length),sales=bookings.flatMap(b=>b.sales||[]);
 const total=sales.reduce((n,s)=>n+Math.round(Number(s.amount||0)*100),0)/100,frames=sales.reduce((n,s)=>n+Number(s.photos||0),0);
 return `<h2>Продажи по съёмкам</h2><p data-sales-totals>В загруженных съёмках: завершённых продаж — ${sales.length}, продано кадров — ${frames}, сумма — ${esc(roubles(total))}. Незавершённых продаж — ${bookings.filter(b=>b.draft).length}.</p>${bookings.map(b=>`<article class="section" data-sale-booking="${b.id}"><strong>№${b.id} · ${esc(bookingTitle(b))}</strong>${b.sales?.length?`<p>Продажа оформлена: ${b.soldTotal} фото · ${esc(roubles(b.salesAmount))}.</p>`:''}${draftProgress(b)}<button class="btn ghost" data-workflow="sale-history" data-booking="${b.id}">${b.draft?'Продолжить продажу':'Редактировать / добавить покупку'}</button></article>`).join('')||'<p>Продаж пока нет.</p>'}`;
}
const field=(label,input)=>`<label class="field">${label}${input}</label>`;
const input=(name,attrs='')=>`<input class="input" name="${name}" ${attrs}>`;
const file=(name,multiple=false,required=true)=>input(name,`type="file" accept="image/jpeg,image/png,image/webp${name==='receipt'?',application/pdf':''}" ${multiple?'multiple':''} ${required?'required':''}`);
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
 if(!/^\/(?:receipts\/\d+\/image|sale-drafts\/\d+\/receipt\/image)(?:\?file=[12])?$/.test(path||''))throw new Error('Некорректная ссылка на чек.');
 clearReceiptViewer();const ticket=receiptVersion,blob=await api(path,{responseType:'blob'});
 if(ticket!==receiptVersion)return;
 const viewer=document.createElement('dialog'),image=document.createElement(blob.type==='application/pdf'?'a':'img'),close=document.createElement('button');
 viewer.className='delivery-viewer';viewer.setAttribute('aria-label','Фото чека');
 close.type='button';close.className='btn';close.textContent='Закрыть';close.onclick=()=>viewer.close();
 receiptURL=URL.createObjectURL(blob);
 if(blob.type==='application/pdf'){image.href=receiptURL;image.target='_blank';image.rel='noopener';image.textContent='Открыть PDF чека';}else{image.src=receiptURL;image.alt='Фото загруженного чека';}
 viewer.append(close,image);viewer.addEventListener('close',clearReceiptViewer,{once:true});
 receiptViewer=viewer;document.body.append(viewer);viewer.showModal();
}

function replacementControl(r){if(r.kind==='sale_disk_selected'&&canReplaceDiskSelection(r))return `<button type="button" class="btn ghost" data-workflow="replace-disk-photo" data-key="${esc(r.key)}">Заменить отклонённый кадр выбранным на Диске</button>`;return canReplaceOperationFile(r)&&r.hasBlob?`<label class="field">${r.kind==='sale_receipt'?'Прикрепить другой чек к этой же продаже':'Исправить отклонённый файл без новой продажи'}<input class="input" type="file" accept="image/jpeg,image/png,image/webp${['sale_receipt','booking_receipt'].includes(r.kind)?',application/pdf':''}" ${['sale_receipt','booking_receipt'].includes(r.kind)?'multiple data-receipt-input':''} data-workflow-replace="${r.key}">${['sale_receipt','booking_receipt'].includes(r.kind)?'<button type="button" class="btn" data-workflow="replace-receipt-files">Отправить исправленный чек</button>':''}</label>`:'';}
function paymentChoice(booking=false){
 if(!booking&&!data.bankConfigured)return input('paymentMethod','type="hidden" value="receipt"');
 return `<label class="field">Способ оплаты<select class="input" name="paymentMethod">${data.bankConfigured?'<option value="bank">Оплата по QR Сбера</option>':''}<option value="receipt">Платёж уже внесён / приложить чек</option>${booking?'<option value="later" '+(!data.bankConfigured?'selected':'')+'>Предоплата позже</option>':''}</select></label>`;
}
function merchantPaymentControl(b){
 if(!data.merchantQRConfigured||data.bankConfigured)return '';
 const available=Number.isSafeInteger(b?.id)&&b.id>0||b?.inline&&isInline(b.scope||b.id);
 return `<section class="notice" data-merchant-payment><h3>Оплата гостем</h3><button type="button" class="btn primary" data-workflow="merchant-qr" data-purpose="PURCHASE" ${available?'':'disabled'}>Показать QR для оплаты остатка</button><p>${available?'Сумма будет сверена с сервером. Подтверждённый аванс учитывается один раз.':'Сначала выберите гостя.'}</p><p>Автоматическое подтверждение по этому QR ещё не подключено. Открытие QR не подтверждает оплату и не открывает фотографии.</p></section>`;
}
const gateStatuses={CREATING:'Уточняем создание счёта',PENDING:'Ожидаем подтверждения банка',PAID:'Банк подтвердил поступление',TEST_PAID:'Тестовая оплата: реальные поступления не подтверждены',NO_PAYMENT_REQUIRED:'Покупка полностью покрыта подтверждённой предоплатой',DECLINED:'Банк отклонил оплату',CANCELLED:'Счёт отменён',REFUNDED:'Платёж возвращён',PARTIAL_REFUND:'Платёж частично возвращён'};
function gateView(row,automatic=false){
 if(!['booking_bank_payment','sale_bank_payment'].includes(row?.kind))return '';
 const bill=row?.result;if(!bill)return '';
 return `<section class="notice" data-auto-bank="${esc(row.key)}"><strong>${row.data?.existingBillId?'Банковский счёт по записи':row.kind==='booking_bank_payment'?'Предоплата по записи':'Оплата покупки'}${bill.amount?' · '+esc(roubles(bill.amount)):''}</strong><p>${bill.testMode?'ТЕСТОВАЯ СРЕДА · ':''}${esc(gateStatuses[bill.status]||bankPaymentStatus(bill))}</p>${!row.error&&row.status!=='error'&&bankPaymentLink(bill)&&bill.qrPath===`/bank-payments/${bill.id}/qr`?`<button type="button" class="btn ghost" data-workflow="payment-qr" data-key="${esc(row.key)}">Показать QR</button><button type="button" class="btn primary" data-workflow="bank-open" data-key="${esc(row.key)}">Оплатить на этом телефоне</button><div data-auto-bank-qr="${esc(row.key)}" ${automatic?'data-auto-show="1"':''}></div>`:''}${row.kind==='sale_bank_payment'&&row.status!=='synced'?'<p>Выбранные фотографии сохранены на этом устройстве. Продажа продолжится после подтверждения банка.</p>':''}</section>`;
}
let automaticQRVersion=0;
const automaticQRs=new Map(),automaticQRLoads=new Map();
function clearAutomaticQR(){automaticQRVersion++;for(const url of automaticQRs.values())URL.revokeObjectURL(url);automaticQRs.clear();automaticQRLoads.clear();}
async function showAutomaticQR(row,box){
 if(!row?.result?.id||row.result.status!=='PENDING'||!box)return;
 const version=automaticQRVersion,actor=data?.actorId,key=`${actor}:${row.key}:${row.result.id}`;
 try{
  let url=automaticQRs.get(key);
  if(!url){
   let pending=automaticQRLoads.get(key);
   if(!pending){pending=api(`/bank-payments/${row.result.id}/qr`,{responseType:'blob'});automaticQRLoads.set(key,pending);}
   const blob=await pending;
   if(version!==automaticQRVersion||actor!==data?.actorId||!box.isConnected)return;
   url=automaticQRs.get(key);if(!url){url=URL.createObjectURL(blob);automaticQRs.set(key,url);}
  }
  if(version!==automaticQRVersion||actor!==data?.actorId||!box.isConnected)return;
  const current=queue.find(item=>item.key===row.key);
  if(!current||current.result?.id!==row.result.id||current.result?.status!=='PENDING')return;
  const image=document.createElement('img');image.src=url;image.alt='QR оплаты Сбера';image.style.maxWidth='320px';image.onerror=()=>{if(version===automaticQRVersion&&box.isConnected){box.textContent='QR не загрузился. Проверьте счёт ещё раз.';box.closest?.('[data-auto-bank]')?.querySelector('[data-workflow="bank-open"]')?.remove();}};box.replaceChildren(image);
 }catch(error){if(version===automaticQRVersion&&box.isConnected){box.textContent=error.message;box.closest?.('[data-auto-bank]')?.querySelector('[data-workflow="bank-open"]')?.remove();}}
 finally{automaticQRLoads.delete(key);}
}
function mountAutomaticQR(){
 for(const box of document.querySelectorAll('[data-auto-bank-qr][data-auto-show]'))void showAutomaticQR(queue.find(row=>row.key===box.dataset.autoBankQr),box);
}
window.addEventListener('hashchange',clearAutomaticQR);window.addEventListener('pagehide',clearAutomaticQR);
window.addEventListener('pb-operations-reset',clearAutomaticQR);document.addEventListener('pb-chat-account-reset',clearAutomaticQR);
function queueView(){
 const inProgress=queue.filter(r=>r.status==='local'||r.status==='syncing').length;
 const errors=queue.filter(r=>r.status==='error'&&!r.supersededByGallery).length;
 const stored=queue.filter(r=>r.status==='synced').length;
 const visible=[...queue.filter(r=>r.status!=='synced').slice(0,50),...queue.filter(r=>r.status==='synced').slice(-20)];
 const summary=`<p class="small">Ожидают отправки или отправляются: ${inProgress} · Ошибки: ${errors} · Принято сервером: ${stored}. Показаны первые 50 незавершённых и последние 20 завершённых операций.</p>`;
 return summary+(visible.map(r=>`<article class="panel"><strong>${r.supersededByGallery?'Продолжено из галереи':r.kind==='shoot_photo'&&r.status==='synced'?'Сервер принял · проверьте Яндекс.Диск ниже':labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.photoId?` · кадр №${r.result.photoId}`:''}${r.filename?` · ${esc(r.filename)}`:''}${r.data?.shooting?.id?` · съёмка №${r.data.shooting.id}`:''}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.retryAt>Date.now()?`<p>Автоматический повтор не раньше ${esc(new Date(r.retryAt).toLocaleTimeString('ru-RU'))}. Остальные файлы отправляются независимо.</p>`:''}${!r.supersededByGallery&&(r.status==='error'||(r.status==='local'&&r.error))?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">${r.autoRetryPaused?'Продолжить отправку':'Повторить'}</button>`:''}${gateView(r)}${replacementControl(r)}</article>`).join('')||'<p>Операций в очереди нет.</p>');
}
function saleTransferProgress(rows){
 const photos=rows.filter(row=>['sale_selected','sale_disk_selected','sale_gallery_selected'].includes(row?.kind));if(!photos.length)return '';
 const accepted=photos.filter(row=>row.status==='synced'),ids=new Set(accepted.map(row=>row.result?.photoId).filter(Number.isSafeInteger));
 const reported=rows.filter(row=>row?.status==='synced'&&Number.isSafeInteger(row.result?.selectedPhotos)).map(row=>row.result.selectedPhotos);
 const confirmed=reported.length?`Сервер подтвердил в продаже разных кадров: ${Math.max(...reported)}.`:`В этой отправке подтверждено разных кадров: ${ids.size}.`;
 return `${confirmed} Файлов принято ${accepted.length} из ${photos.length}; повторных копий: ${accepted.filter(row=>row.result?.duplicate).length}. Ожидают отправки: ${photos.filter(row=>row.status==='local').length}; передаются: ${photos.filter(row=>row.status==='syncing').length}; ошибки файлов: ${photos.filter(row=>row.status==='error').length}.`;
}
function connectionNote(){
 const offline=data?._offline?(`${navigator.onLine?'Сеть доступна, но справочники взяты из локальной копии.':'Нет сети. Справочники сохранены.'} ${new Date(data._cachedAt).toLocaleString('ru-RU')}. `):'';
 return offline+'При закрытии или сворачивании приложения телефон может приостановить передачу. После открытия отправка продолжится из сохранённой очереди. Фотографии сначала попадают в очередь именно этого браузера на этом устройстве. Пока сервер не принял их, не очищайте данные браузера. После передачи проверяйте отдельный статус «Сохранено на Яндекс.Диске».';
}
function inlineStartData(form,b){
 const f=new FormData(form),guest={name:String(f.get('guest_name')||'').trim(),hotelId:Number(f.get('hotel_id')),photographerId:Number(f.get('photographer_id'))||undefined};
 for(const [field,key] of [['guest_phone','phone'],['guest_room','room'],['guest_date','date'],['guest_time','time']])if(f.get(field))guest[key]=String(f.get(field)).trim();
 if(f.get('package_id'))guest.packageId=Number(f.get('package_id'));
 const tariffId=Number(f.get('tariffId'))||null;
 if(!guest.name||!guest.hotelId||!guest.photographerId||!b.price)throw new Error('Укажите имя гостя, отель, реального фотографа и тариф.');
 return {guest,price:b.price,...(tariffId?{tariffId}:{})};
}
function inlineMerchantStartData(form,b){
 const start=inlineStartData(form,b),tariff=b.draft?.pricing?.tariffId||Number(new FormData(form).get('tariffId'));
 if(tariff>0)start.tariffId=tariff;
 return start;
}
function merchantPreparationOnly(rows){return rows.length===1&&rows[0]?.kind==='sale_start_inline'&&rows[0]?.merchantPreparation===true;}
function clearMerchantPreparationKeys(form){
 if(form?.id!=='workflowSale'||!isInline(form.dataset.draftScope)||!form.dataset.pendingKeys)return;
 let keys;try{keys=JSON.parse(form.dataset.pendingKeys);}catch{return;}
 if(!Array.isArray(keys)||keys.length!==1||keys[0]!==form.dataset.draftScope.slice(7))return;
 const rows=queue.filter(row=>row.key===keys[0]||row.kind?.startsWith('sale_')&&row.data?.draft?.key===keys[0]);
 if(merchantPreparationOnly(rows)){delete form.dataset.pendingKeys;captureForm(form);}
}
function requireSameInlinePreparation(start,startData){
 if(start&&JSON.stringify(start.data)!==JSON.stringify(startData))throw new Error('Данные гостя уже сохранены для этой оплаты. Верните их к сохранённым значениям или начните продажу другого гостя.');
}
async function prepareMerchantInline(form,isCurrent){
 if(merchantPreparations.has(form))return merchantPreparations.get(form);
 const pending=(async()=>{
  const b=saleFormBooking(form),scope=form.dataset.draftScope,key=scope.slice(7),actor=data.actorId,startData=inlineMerchantStartData(form,b);
  let rows=await outboxRows();if(!isCurrent())return null;
  let start=rows.find(row=>row.key===key);
  if(start&&(start.kind!=='sale_start_inline'||start.userId!==actor))throw new Error('Запись оплаты изменилась. Обновите приложение.');
  requireSameInlinePreparation(start,startData);
  if(!start)await enqueueBatch([{key,kind:'sale_start_inline',date:todayKey(),data:startData,merchantPreparation:true}]);
  else if(start.status==='error'||start.status==='local'&&start.error)await retryOperation(key);
  if(!isCurrent())return null;
  await flushOutbox();
  const until=Date.now()+25000;
  while(isCurrent()){
   rows=await outboxRows();if(!isCurrent())return null;queue=rows;
   start=rows.find(row=>row.key===key);
   if(!start||start.kind!=='sale_start_inline'||start.userId!==actor)throw new Error('Запись оплаты не найдена. Проверьте журнал синхронизации.');
   if(start.status==='synced'&&Number.isSafeInteger(start.result?.bookingId)&&Number.isSafeInteger(start.result?.draftId)){
    requireSameInlinePreparation(start,inlineMerchantStartData(form,saleFormBooking(form)));
    return start;
   }
   if(start.status==='error'||start.status==='local'&&start.error||Date.now()>=until||typeof navigator!=='undefined'&&navigator.onLine===false)throw new Error(start.error||'Запись гостя сохранена в очереди. Дождитесь связи и откройте QR снова; повторная запись не создаётся.');
   await new Promise(resolve=>setTimeout(resolve,100));
  }
  return null;
 })();
 merchantPreparations.set(form,pending);
 try{return await pending;}finally{if(merchantPreparations.get(form)===pending)merchantPreparations.delete(form);}
}
function inlineGuestFields(b){
 const cfg=data.inlineSale||{},hotels=data.hotels.filter(row=>cfg.hotelIds?.includes(row.id)),hotel=hotels.find(row=>row.id===b.hotelId);
 const people=(cfg.photographers||[]).filter(row=>!b.hotelId||row.hotelIds.includes(b.hotelId));
 const photographer=cfg.photographerLocked?input('photographer_id',`type="hidden" value="${cfg.defaultPhotographerId||''}"`)+`<p>Фотограф: ${esc(people.find(row=>row.id===cfg.defaultPhotographerId)?.name||'Вы')}</p>`:field('Фотограф съёмки',`<select class="input" name="photographer_id" required><option value="">Выберите фотографа</option>${people.map(row=>`<option value="${row.id}" ${row.id===b.inlineGuest?.photographerId?'selected':''}>${esc(row.name)}</option>`).join('')}</select>`);
 const hotelControl=hotels.length===1?input('hotel_id',`type="hidden" value="${hotels[0].id}"`)+`<p>Отель: ${esc(hotels[0].name)}</p>`:field('Отель',`<select class="input" name="hotel_id" required><option value="">Выберите отель</option>${hotels.map(row=>`<option value="${row.id}" ${row.id===b.hotelId?'selected':''}>${esc(row.name)}</option>`).join('')}</select>`);
 const ambiguous=hotel?.unitPrice==null&&new Set(data.packages.map(row=>String(row.price))).size>1;
 return `${field('Имя гостя',input('guest_name',`required minlength="1" maxlength="200" value="${esc(b.inlineGuest?.name||'')}"`))}${hotelControl}${photographer}${ambiguous?field('Тариф',`<select class="input" name="package_id" required><option value="">Выберите тариф</option>${data.packages.map(row=>`<option value="${row.id}">${esc(row.name)} · ${esc(row.price)} ₽/кадр</option>`).join('')}</select>`).replace('<label class="field">','<label class="field" data-inline-package>'):''}`;
}
function saleFormView(b,d){
 const count=d?.sold||'',total=b.inline?'':frameTotal(b),known=Number(total)>0;
 const pricing=tariffOptions(b,data.tariffs),preview=pricing.match(/<div class="notice" id="salePricePreview"[\s\S]*$/)?.[0]||'';
 const extras=pricing.replace(preview,'');
 return `<form id="workflowSale" data-draft-scope="${b.scope||b.id}" class="form-grid section sale-save-form">
 ${b.inline?inlineGuestFields(b):`<div class="sale-context"><strong>${esc(b.client||'Гость не указан')}</strong><span>${esc(b.hotel||'Отель не указан')} · ${esc(b.date||'Дата не указана')} ${esc(b.time||'')}</span><span>${esc(b.photographer||'Фотограф не указан')} · комната ${esc(b.room||'—')}${b.phone?' · '+esc(b.phone):''}</span></div>`}
 ${b.inline?'':'<label class="field"><input type="radio" name="photoSource" value="device" checked> Загрузить с устройства</label>'}
 ${field('Выбранные фотографии',file('selected',true,false))}
 ${b.inline?'':gallerySaleView(b,data)}
 <div class="sale-save-status" data-operation-progress role="status" aria-live="polite">${d?.selected?`Сервер уже принял ${d.selected} фото. Выберите оставшиеся снимки.`:'Выберите снимки. Передача начнётся после нажатия «Сохранить продажу».'}</div>
 <p data-sale-count-summary>${b.inline?'Фото этой продажи: по выбранным файлам.':known?`Всего в съёмке: ${total}. Количество покупки — по выбранным файлам.`:'Количество покупки — по выбранным файлам. Укажите общее число кадров в деталях, если оно ещё неизвестно.'}</p>
 ${b.inline||known?input('total',`type="hidden" value="${total}"`):''}
 ${preview}
 <details class="sale-details" ${!b.inline&&!known?'open':''}><summary>Детали, альбом и исправления</summary>
 ${b.inline?`${field('Телефон (необязательно)',input('guest_phone','type="tel" maxlength="80"'))}${field('Комната (необязательно)',input('guest_room','maxlength="100"'))}${field('Дата',input('guest_date',`type="date" value="${b.date}"`))}${field('Время',input('guest_time',`type="time" value="${b.time||''}"`))}`:!known?field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${total}"`)):''}
 <div data-sale-pricing-controls>${extras}</div>${field('Количество покупки — исправить',input('sold',`type="number" min="1" max="10000" value="${count}" data-sale-auto-count`))}<div id="saleDiscountSlot"></div>
 ${b.inline?'':`<details class="sale-extra-source"><summary>Выбрать снимки по ссылке или на Яндекс.Диске</summary>${saleDiskView(b,data)}</details>`}
 </details>
 ${paymentChoice()}${merchantPaymentControl(b)}
 ${!d||d.status==='AWAITING_RECEIPT'?field(b.sales?.length?'Новый чек доплаты (не прежний чек)':'Чек продажи',file('receipt',false,false)):''}
 <button class="btn primary sale-save-button" type="submit">Сохранить продажу</button>
 <p class="sale-field-help">Фото и данные отправятся автоматически. Готовая ссылка появится после подтверждения сервера и выполнения правил оплаты.</p>
 <button type="button" class="btn ghost" data-workflow="another-sale">Другая съёмка / новый гость</button>
 </form>`;
}
function recoverSaleQueue(form){
 if(form?.id!=='workflowSale')return;
 clearMerchantPreparationKeys(form);if(form.dataset.pendingKeys){try{form.dataset.pendingKeys=JSON.stringify(galleryRecoveryKeys(queue,JSON.parse(form.dataset.pendingKeys)));}catch{}return;}
 const scope=form.dataset.draftScope,booking=Number(scope),starts=queue.filter(row=>isSaleStart(row)&&(row.data?.booking?.id===booking||row.result?.bookingId===booking||'inline:'+row.key===scope));
 for(const start of starts.reverse()){
  const rows=queue.filter(row=>row.key===start.key||(row.kind.startsWith('sale_')&&row.data?.draft?.key===start.key));
  if(merchantPreparationOnly(rows))continue;
  const recovered=galleryRecoveryKeys(queue,rows.map(row=>row.key));
  if(recovered.some(key=>!rows.some(row=>row.key===key))){form.dataset.pendingKeys=JSON.stringify(recovered);captureForm(form);return;}

  if(rows.length&&rows.some(row=>row.status!=='synced')){
   form.dataset.pendingKeys=JSON.stringify(rows.map(row=>row.key));captureForm(form);return;
  }
 }
}
function syncSaleSelection(form){
 if(form?.id!=='workflowSale'||form.dataset.pendingKeys||(isInline(form.dataset.draftScope)?form.dataset.draftScope:Number(form.dataset.draftScope))!==selected||!form.elements?.sold)return;
 const b=currentBooking(form);if(!b)return;
 let chosen=0;
 if((form.elements.photoSource?.value||'device')==='device')chosen=form.elements.selected?.files?.length||0;
 else try{chosen=form.elements.photoSource?.value==='gallery'?selectedGalleryPhotos(form).length:selectedDiskPhotos(form).length;}catch{ /* Source is not connected yet. */ }
 const saved=Number(b.draft?.selected||0),fixed=b.draft?.status==='AWAITING_SELECTED'&&Number(b.draft?.sold)>0;
 if(!fixed)form.elements.sold.value=String(saved+chosen);
 if(b.inline)form.elements.total.value=String(saved+chosen);
 const summary=form.querySelector('[data-sale-count-summary]');if(summary)summary.textContent=b.inline?`Фото этой продажи: ${saved+chosen}.`:`${frameTotal(b)?'Всего в съёмке: '+frameTotal(b)+'. ':''}В этой покупке: ${saved+chosen} фото.`;
 const note=form.querySelector('[data-operation-progress]');
 if(note)note.textContent=`Выбрано ${chosen} новых фото.${saved?` Ранее сервер принял ${saved}.`:''} Файлы ещё не переданы. Нажмите «Сохранить продажу» — передача начнётся автоматически.`;
 captureForm(form);refreshQuote();
}
function currentSaleState(rows,b){
 const state=saleSaveState(rows),fresh=b?.gallery;
 if(state.accepted&&fresh?.saleId===state.result.saleId){
  state.gallery=fresh;state.ready=Boolean(fresh.ready===true&&fresh.state==='ready'&&fresh.clientUrl);
 }
 return state;
}
function saleGalleryView(form,state){
 let box=form.querySelector('[data-sale-gallery]');
 if(!state.accepted){box?.remove();return;}
 const gallery=state.gallery;
 if(!box){box=document.createElement('section');box.dataset.saleGallery='';box.className='sale-gallery-result';form.append(box);}
 const signature=JSON.stringify([state.result.saleId,state.ready,gallery?.clientUrl,gallery?.qrPath,gallery?.syncPath,gallery?.state]);
 if(box.dataset.signature===signature)return;box.dataset.signature=signature;
 if(!state.ready){
  const messages={payment_pending:'Фотографии сохранены. Гостевая ссылка откроется после подтверждения оплаты.',storage_pending:'Фотографии ещё не подтверждены на подключённом Диске. Проверьте полученные фото этой сохранённой продажи; гостевая ссылка пока недоступна.',closed:'Выдача этой галереи закрыта. Проверьте настройки доступа в клиентской галерее.',expired:'Срок доступа к этой галерее истёк. Проверьте срок в настройках клиентской галереи.'};
  const sync=gallery?.state==='storage_pending'&&gallery.syncPath;
  box.innerHTML=`<strong>Продажа №${state.result.saleId} принята сервером</strong><p>${esc(messages[gallery?.state]||'Галерея ещё не подтверждена сервером. Проверьте состояние сохранённой продажи.')}</p><button type="button" class="btn ghost" data-workflow="${sync?'gallery-sync':'gallery-status'}">${sync?'Проверить полученные фото':'Проверить состояние галереи'}</button><p data-sale-gallery-message role="status"></p>`;
  return;
 }
 let link;try{link=new URL(gallery.clientUrl,location.origin);if(!['https:','http:'].includes(link.protocol)||link.username||link.password||!/^\/g\/[A-Za-z0-9_-]{32}$/.test(link.pathname))throw Error();}catch{box.textContent='Сервер подтвердил галерею, но ссылка требует проверки. Обновите состояние.';return;}
 box.innerHTML=`<strong>Галерея готова · ${Number(gallery.photoCount||state.result.soldPhotos||0)} фото</strong><a class="btn" href="${esc(link.href)}" target="_blank" rel="noopener noreferrer">Открыть галерею гостя</a><label class="field">Ссылка гостя<input class="input" readonly value="${esc(link.href)}" data-sale-client-link></label><div class="row"><button type="button" class="btn ghost" data-workflow="copy-gallery">Копировать ссылку</button>${gallery.qrPath?'<button type="button" class="btn ghost" data-workflow="gallery-qr">Показать QR гостю</button>':''}</div><div data-sale-gallery-qr></div><p data-sale-gallery-message role="status"></p>`;
}
function clearSaleQRs(){for(const url of saleQRs.values())URL.revokeObjectURL(url);saleQRs.clear();}
function bookingChain(key,rows=queue){const booking=rows.find(row=>row.key===key&&row.kind==='booking');return booking?[booking,...rows.filter(row=>['booking_receipt','booking_bank_payment'].includes(row.kind)&&row.data?.booking?.key===key)]:[];}
export function locallySavedBooking(rows,actor){
 const booking=rows[0];
 return Boolean(Number.isSafeInteger(actor)&&actor>0&&booking?.kind==='booking'&&rows.every(Boolean)&&
  new Set(rows.map(row=>row.key)).size===rows.length&&rows.every(row=>typeof row.key==='string'&&row.key.length>=16&&row.key.length<=80&&row.userId===actor&&
   (row.generation||'')===(booking.generation||'')&&['local','syncing','synced','error'].includes(row.status))&&
  rows.slice(1).every(row=>['booking_receipt','booking_bank_payment'].includes(row.kind)&&row.data?.booking?.key===booking.key&&
   (row.kind!=='booking_receipt'||row.status==='synced'||row.hasBlob===true)));
}
function sameBookingData(row,fields){return row?.kind==='booking'&&Object.keys(row.data||{}).length===Object.keys(fields).length&&Object.entries(fields).every(([key,value])=>row.data[key]===value);}
function bookingHistoryView(){
 const rows=queue.filter(row=>row.kind==='booking').slice(-20);
 return rows.map(row=>{
  const chain=bookingChain(row.key),repair=rejectedBooking(chain),hotel=data?.hotels.find(h=>h.id===row.data?.hotel_id);
  const state=bookingAccepted([row])?`Запись №${row.result.bookingId} сохранена сервером`:row.status==='error'?'Запись требует исправления':row.status==='syncing'?'Отправляется на сервер':'Сохранена на устройстве · ожидает отправки';
  const receipt=chain.find(step=>step.kind==='booking_receipt');
  return `<article class="panel" data-booking-history="${esc(row.key)}"><strong>${esc(row.data?.client_name||'Гость')}</strong><p>${esc(hotel?.name||'Отель №'+row.data?.hotel_id)} · ${esc(row.data?.shoot_date)} ${esc(row.data?.shoot_time)} · комната ${esc(row.data?.room)}</p><p role="status">${state}</p>${row.error?`<p role="alert">${esc(row.error)}</p>`:''}${receipt?`<p>${receipt.status==='synced'?'Чек передан на проверку. Поступление подтверждается отдельно.':'Чек сохранён в очереди этой брони. Поступление ещё не подтверждено.'}</p>`:''}${repair?`<button type="button" class="btn ghost" data-workflow="correct-queued-booking" data-key="${esc(row.key)}">Исправить запись</button>`:''}</article>`;
 }).join('');
}
function refreshBookingHistory(){for(const selector of ['#bookingHistory','[data-quick-booking-history]']){const target=document.querySelector(selector);if(target)target.innerHTML=bookingHistoryView();}}
function closeBookingCorrection(){bookingCorrection?.remove();bookingCorrection=null;}
async function correctQueuedBooking(key){
 const actor=data?.actorId,rows=await outboxRows(),chain=bookingChain(key,rows),booking=rejectedBooking(chain);
 if(actor!==data?.actorId||!booking||!locallySavedBooking(chain,actor))throw Error('Эту запись сейчас нельзя исправить. Проверьте её актуальное состояние.');
 captureForms();closeBookingCorrection();const dialog=document.createElement('dialog');bookingCorrection=dialog;
 dialog.className='panel';dialog.style.cssText='width:min(600px,calc(100vw - 32px));max-height:calc(100dvh - 32px);overflow:auto';
 dialog.innerHTML=`<h2>Исправить сохранённую бронь</h2><p>Новая запись не создаётся. Прежний чек остаётся связан с этой же бронью.</p><form id="workflowBookingCorrection" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required><option value="">Выберите отель</option>${options(data.hotels)}</select>`)}${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" required'))}${field('Дата',input('shoot_date','type="date" required'))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required><option value="">Выберите пакет</option>${options(data.packages)}</select>`)}${field('Предоплата по записи, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" required'))}<p role="alert"></p><button class="btn primary" type="submit">Сохранить исправление</button><button class="btn ghost" type="button" data-close-booking>Закрыть</button></form>`;
 const form=dialog.querySelector('form');for(const [name,value] of Object.entries(booking.data))if(form.elements[name])form.elements[name].value=value??'';
 form.querySelector('[role=alert]').textContent=booking.error;
 form.querySelector('[data-close-booking]').addEventListener('click',closeBookingCorrection);
 form.addEventListener('submit',async event=>{
  event.preventDefault();const button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;
  try{
   if(actor!==data?.actorId||bookingCorrection!==dialog)throw Error('Аккаунт изменился. Откройте исправление заново.');
   await repairBookingQueue(chain.map(row=>row.key),{...bookingFormData(form),photographer_id:booking.data.photographer_id??null});
   if(actor!==data?.actorId)return;queue=await outboxRows();refreshBookingHistory();closeBookingCorrection();
  }catch(error){if(form.isConnected)form.querySelector('[role=alert]').textContent=error.message;}
  finally{if(button.isConnected)button.disabled=false;}
 });
 document.body.append(dialog);dialog.showModal();
}
async function restoreCommittedBooking(){
 const form=document.querySelector('#workflowBooking');if(!form||form.dataset.resetting==='1')return;
 let chain=[];
 if(form.dataset.pendingKeys){try{chain=JSON.parse(form.dataset.pendingKeys).map(key=>queue.find(row=>row.key===key));}catch{return;}}
 else if(form.dataset.bookingIntentKey){chain=bookingChain(form.dataset.bookingIntentKey);if(!sameBookingData(chain[0],bookingFormData(form)))return;}
 if(!locallySavedBooking(chain,data?.actorId))return;
 form.dataset.pendingKeys=JSON.stringify(chain.map(row=>row.key));captureForm(form);
 try{const next=await resetCompletedForm(form,{refresh:false});if(next)revealBookingForm(next);}
 catch(error){const note=document.querySelector('#workflowSyncNote');if(note)note.textContent=error.message;}
}
export function workflowView({includeBooking=!quickBooking}={}){
 const ready=data.bookings.filter(saleable),b=currentBooking(),d=b?.draft;
 const upload=data.bookings.filter(b=>b.canFullUpload??(b.status==='READY_FOR_SALE'&&!b.fullUploaded&&b.canUpload));
 return `<h1>Рабочие операции</h1>${receiptReviewButton(data.canAuditDisk?['OWNER']:[])}<div id="workflowNotice" class="notice">${esc(connectionNote())}</div>
 ${pricingView(data)}${diskFolderPanel(data)}${data.canBook&&includeBooking?`<details class="panel section"><summary>Новая бронь</summary><div id="bookingHistory" aria-label="Сохранённые на этом устройстве брони">${bookingHistoryView()}</div><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}<p id="bookingFixedPrice" class="notice">${esc(bookingPriceNote(data,data.hotels[0]?.id))}</p>${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Предоплата по записи, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="1000" required'))}<p>Обычно 1000 ₽; укажите другую сумму, если договорились иначе. Без подтверждения поступления эта сумма не уменьшает доплату.</p>${paymentChoice(true)}${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа и допродажа</h2><form id="workflowLookup" class="row"><label>Номер записи <input class="input" name="bookingId" type="number" min="1" required></label><button class="btn ghost" type="submit">Найти прежнюю съёмку</button></form><p>Цена одного кадра закреплена за записью и не меняется при продаже. Можно выбрать пакет этого отеля. Сумма продажи, подтверждённая предоплата и остаток показываются отдельно.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${data.inlineSale?.hotelIds?.length?'<option value="-1">Новый гость — без записи</option>':''}${saleSelectionOptions()}${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(bookingTitle(x))}</option>`).join('')}</select><div id="workflowSelectedSaleSummary">${b?salesHistory(b)+draftProgress(b):''}</div>${b?(d&&!d.mine?`<p>Продажу оформляет ${esc(d.createdByName||'другой сотрудник')}. Сохранено выбранных кадров: ${d.selected||0} из ${d.sold||'—'}.</p>${data.canAuditDisk?`<p>Владелец может продолжить этот черновик. Чек, согласованная цена и принятые фотографии сохранятся; прежний сотрудник больше не сможет изменять эту продажу.</p><button type="button" class="btn primary" data-workflow="takeover-sale" data-booking="${b.id}">Продолжить продажу как владелец</button>`:''}`:saleFormView(b,d)):''}</section>
 <details class="panel section"><summary>Дополнительные банковские операции</summary>${bankConnectionPanel(data)}${bankPanel(b?.inline?null:b,data)}</details><section class="panel section" id="workflowSalesSummary">${salesOverview()}</section>
 <details class="panel section"><summary>Загрузить полную съёмку</summary>${upload.length?`<form id="workflowShoot" class="form-grid">${field('Съёмка',`<select class="input" name="booking">${options(upload)}</select>`)}${field('Фотографии',file('photos',true,false))}<label><input type="checkbox" name="finish"> Все кадры добавлены — завершить загрузку</label><p>Можно добавлять частями. Ставка фотографа фиксируется сервером после завершения полной загрузки.</p><button class="btn primary">Сохранить фотографии</button></form><label class="field section"><span>Выбрать папку — отправка сразу, по одному файлу</span><input id="workflowShootFolder" class="input" type="file" accept="image/jpeg,image/png,image/webp" webkitdirectory multiple></label><p id="workflowFolderProgress" class="notice" role="status">После выбора папки не закрывайте вкладку до конца передачи. Проверяйте Яндекс.Диск отдельно.</p>`:'<p>Нет доступных съёмок для полной загрузки.</p>'}</details>
 <section class="panel section"><h2>Файлы на Яндекс.Диске</h2><button class="btn ghost" data-workflow="refresh">Обновить состояние файлов</button>${data.bookings.filter(b=>b.shootingId).map(b=>`<details data-storage-shooting="${b.shootingId}"><summary>${esc(bookingTitle(b))} · сохранено ${(b.files||[]).filter(f=>f.status==='STORED').length} из ${(b.files||[]).length}</summary>${(b.files||[]).map(f=>`<p>Кадр №${f.id}: ${esc(({STORED:'Сохранено на Диске',PENDING:'Ожидает сохранения',UPLOADING:'Сохраняется',FAILED:'Ошибка сохранения'})[f.status])} ${esc(f.error||'')}</p>`).join('')}${editView(b)}${b.canUpload?`<button class="btn ghost" data-workflow="retry-storage" data-shooting="${b.shootingId}">Восстановить сохранение на Диске</button>`:''}</details>`).join('')||'<p>Съёмок пока нет.</p>'}</section>
 <section class="section"><h2>Синхронизация</h2><button class="btn ghost" data-workflow="sync">Синхронизировать сейчас</button><div id="workflowSyncNote" class="notice" role="status">Отправка запускается автоматически при наличии сети. Нажмите кнопку для ручной проверки.</div><div id="workflowQueue">${queueView()}</div></section>`;
}
function imageFiles(form,name,maximum=20*1024*1024){const list=name==='receipt'?receiptFiles(form.elements[name]):[...form.elements[name].files];if(name==='receipt'&&list.length>2)throw Error('Выберите не больше двух файлов чека.');for(const f of list)if(!(['image/jpeg','image/png','image/webp',...(name==='receipt'?['application/pdf']:[])].includes(f.type)||(!f.type&&(name==='receipt'?/\.(jpe?g|png|webp|pdf)$/i:/\.(jpe?g|png|webp)$/i).test(f.name)))||!f.size||f.size>maximum)throw new Error(`Нужен ${name==='receipt'?'чек JPEG, PNG, WebP или одностраничный PDF':'JPEG, PNG или WebP'} не больше ${maximum/1024/1024} МБ.`);return list;}
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
export function workflowSubmit(form){
 if(submittingForms.has(form))return submittingForms.get(form);
 form.setAttribute('aria-busy','true');
 const pending=submitWorkflow(form).finally(()=>{submittingForms.delete(form);form.removeAttribute('aria-busy');});
 submittingForms.set(form,pending);return pending;
}
async function submitWorkflow(form){
 const actor=data?.actorId;if(form.id==='workflowSale')saleFormBooking(form,actor);
 if(form.id==='workflowSale'){
  if(merchantPreparations.has(form))await merchantPreparations.get(form);
  saleFormBooking(form,actor);queue=await outboxRows();saleFormBooking(form,actor);clearMerchantPreparationKeys(form);
 }
 captureForm(form);
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=(await outboxRows()).filter(r=>keys.includes(r.key));
  if(rows.length!==keys.length)throw new Error('Часть очереди не найдена. Не создавайте повторную продажу; проверьте журнал синхронизации.');
  if(form.id==='workflowBooking'&&form.dataset.repairingBooking!=='1'&&locallySavedBooking(keys.map(key=>rows.find(row=>row.key===key)),actor)){const next=await resetCompletedForm(form,{refresh:false});if(next)revealBookingForm(next);return;}
  if(rows.every(r=>r.status==='synced'))throw new Error('Эта операция уже сохранена. Для новой используйте кнопку под результатом.');
  if(form.id==='workflowBooking'&&form.dataset.repairingBooking==='1'){
   await repairBookingQueue(keys,bookingFormData(form));delete form.dataset.repairingBooking;captureForm(form);
   queue=await outboxRows();updateFormProgress();return;
  }
  if(form.id==='workflowSale'&&form.dataset.repairingInline==='1'){
   if(!rejectedInlineSale(rows))throw new Error('Исправление недоступно: проверьте результат прежней отправки.');
   const context=selectionContext();if(!await loadWorkflow())return;
   if(!selectionCurrent(context)||!form.isConnected)throw new Error('Аккаунт или форма изменились.');
   const b=saleFormBooking(form,actor),startData=inlineStartData(form,b);
   await repairInlineSaleQueue(keys,startData);delete form.dataset.repairingInline;captureForm(form);
   queue=await outboxRows();updateFormProgress();return;
  }
  if(form.id==='workflowSale'&&form.dataset.galleryRecovery==='1'){
   const context=selectionContext(),bid=currentBooking(form)?.id;
   const fresh=await api('/workflow?booking='+bid,{allowSnapshot:false});
   if(!selectionCurrent(context)||!form.isConnected||fresh.actorId!==actor)throw Error('Аккаунт изменился. Откройте продажу заново.');
   data=paymentMode(fresh);const b=currentBooking(form),draft=b?.draft;
   if(!draft?.mine||draft.status!=='AWAITING_SELECTED')throw Error('Продажа уже изменилась. Обновите её состояние перед продолжением.');
   const selected=selectedGalleryPhotos(form),remaining=Number(draft.sold)-Number(draft.selected||0);
   if(selected.length!==remaining||remaining<1)throw Error(`Отметьте ровно ${Math.max(0,remaining)} ещё не принятых купленных кадров из галереи.`);
   const added=await recoverSaleFromGallery(keys,{draftId:draft.id},selected);
   const retained=rows.filter(row=>row.status==='synced'&&row.kind!=='sale_complete').map(row=>row.key);
   form.dataset.pendingKeys=JSON.stringify([...retained,...added.map(row=>row.key)]);delete form.dataset.galleryRecovery;captureForm(form);
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
 if(form.id==='workflowBooking'){
  const rows=await outboxRows(),existing=rows.find(row=>row.key===form.dataset.bookingIntentKey);
  if(existing){
   if(!sameBookingData(existing,bookingFormData(form)))throw Error('Эта бронь уже сохранена с другими данными. Проверьте её карточку перед изменениями.');
   const chain=bookingChain(existing.key,rows);if(!locallySavedBooking(chain,actor))throw Error('Не удалось подтвердить местное сохранение этой брони. Данные формы сохранены.');
   form.dataset.pendingKeys=JSON.stringify(chain.map(row=>row.key));captureForm(form);queue=rows;
   const next=await resetCompletedForm(form,{refresh:false});if(next)revealBookingForm(next);return;
  }
 }
 const f=new FormData(form),date=todayKey(),items=[],reusedKeys=[],add=(kind,data,blob=null,key=crypto.randomUUID())=>{const row={key,kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.classList.contains('photo-edit')){add('photo_edit',{shooting:Number(form.dataset.shooting),photo:Number(form.dataset.photo),action:'MANUAL',edit:null,parameters:Object.fromEntries(['exposure','contrast','color','red','blue'].map(k=>[k,Number(f.get(k))]))});
 }else if(form.id==='workflowBooking'){
  const receipt=(imageFiles(form,'receipt',8*1024*1024).length?imageFiles(form,'receipt',8*1024*1024):null),method=f.get('paymentMethod')||(receipt?'receipt':data.bankConfigured?'bank':'later');
  if(method==='bank'&&receipt)throw new Error('Для уже внесённой предоплаты выберите оплату с чеком.');
  if(method==='bank'&&!data.bankConfigured)throw new Error('Оплата по QR пока не настроена. Выберите другой способ оплаты.');
  const bookingData=bookingFormData(form),booking=add('booking',bookingData,null,form.dataset.bookingIntentKey);
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
  if(method==='bank'&&Number(bookingData.deposit)>0)add('booking_bank_payment',{booking,payment:{}});
 }else if(form.id==='workflowSale'){
  const b=saleFormBooking(form,actor);const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const source=form.elements.photoSource?.value||'device',useDisk=source!=='device',galleryPhotos=source==='gallery'?selectedGalleryPhotos(form):[],diskPhotos=useDisk&&source!=='gallery'?selectedDiskPhotos(form):[],photos=useDisk?[]:imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold+(b.soldTotal||0)>total)throw new Error('Проверьте количество кадров.');
  if(sold<(d?.selected||0))throw new Error(`Уже принято ${d.selected} выбранных фото. Проданное количество не может быть меньше.`);
  if(photos.length+diskPhotos.length+galleryPhotos.length>sold||photos.length+diskPhotos.length+galleryPhotos.length+(d?.selected||0)<sold)throw new Error(`Нужно прикрепить ${Math.max(0,sold-(d?.selected||0))} новых выбранных кадров. Повторные фотографии не увеличивают количество.`);
  const priceQuote=updatePricePreview(form,b,data.tariffs);
  if(!priceQuote)throw new Error('Проверьте сумму продажи фотографий и количество кадров.');
  await checkSaleFiles(photos);saleFormBooking(form,actor);
  const startData=b.inline?inlineStartData(form,b):{booking:{id:b.id},price:b.price},startTariff=d?.pricing?.tariffId||Number(f.get('tariffId'));
  if(startTariff>0)startData.tariffId=startTariff;
  if(b.inline&&(!startData.guest.name||!startData.guest.hotelId||!startData.guest.photographerId||!b.price))throw new Error('Укажите имя гостя, отель, реального фотографа и тариф.');
  const prepared=b.inline?queue.find(row=>row.key===String(selected).slice(7)&&row.kind==='sale_start_inline'&&row.merchantPreparation===true):null;
  if(prepared)requireSameInlinePreparation(prepared,startData);
  const draft=prepared?{key:prepared.key}:add(b.inline?'sale_start_inline':'sale_start',startData);
  if(prepared)reusedKeys.push(prepared.key);
  else if(b.inline){items[items.length-1].key=String(selected).slice(7);draft.key=items[items.length-1].key;}
  const receipt=(!d||d.status==='AWAITING_RECEIPT')?(imageFiles(form,'receipt',8*1024*1024).length?imageFiles(form,'receipt',8*1024*1024):null):null;
  const method=f.get('paymentMethod')||(receipt?'receipt':data.bankConfigured?'bank':'receipt');
  const counts={draft,total,sold,price:b.price,tariffId:d?.pricing?.tariffId||Number(f.get('tariffId'))||null,discountId:d?.pricing?.discountId||Number(f.get('discountId'))||null,albumId:Number(f.get('albumId'))||null,albumQuantity:f.get('albumId')?Number(f.get('albumQuantity')||1):null,...(priceQuote.photoAmount!=null?{photoAmount:priceQuote.photoAmount}:{})};
  if(method==='bank'){
   if(receipt)throw new Error('Для уже внесённой оплаты выберите оплату с чеком.');
   if(!data.bankConfigured)throw new Error('Оплата по QR пока не настроена. Выберите другой способ оплаты.');
   add('sale_bank_payment',{draft,booking:b.inline?draft:{id:b.id},payment:Object.fromEntries(Object.entries(counts).filter(([key])=>!['draft','price'].includes(key)))});
  }else if(receipt)add('sale_receipt',{draft},receipt);
  else if(!d||d.status==='AWAITING_RECEIPT'){
   const quote=updatePricePreview(form,b,data.tariffs);
   if(!quote||!Number.isFinite(quote.due))throw new Error('Проверьте сумму продажи и остаток к оплате.');
   if(quote.due>0)throw new Error('Прикрепите чек продажи на остаток к оплате или выберите оплату по QR.');
  }
  if(!d||d.status!=='AWAITING_SELECTED'||total!==Number(d.total)||sold!==Number(d.sold))add('sale_counts',counts);
  for(const photo of photos)add('sale_selected',{draft},photo);
  for(const photo of diskPhotos)add('sale_disk_selected',{draft,...photo});
  for(const photo of galleryPhotos)add('sale_gallery_selected',{draft,...photo});
  add('sale_complete',{draft,price:b.price});
 }else{
  const b=data.bookings.find(b=>b.id===Number(f.get('booking')));if(!b)throw new Error('Выберите съёмку.');
  for(const photo of imageFiles(form,'photos'))add('shoot_photo',{shooting:{id:b.shootingId}},photo);
  if(f.get('finish'))add('shoot_complete',{shooting:{id:b.shootingId}});
 }
 if(form.id==='workflowSale')saleFormBooking(form,actor);
 try{await enqueueBatch(items);}catch(error){
  if(form.id==='workflowBooking'&&error?.name==='ConstraintError'){
   const rows=await outboxRows(),chain=bookingChain(form.dataset.bookingIntentKey,rows);
   if(sameBookingData(chain[0],bookingFormData(form))&&locallySavedBooking(chain,actor)){
    form.dataset.pendingKeys=JSON.stringify(chain.map(row=>row.key));captureForm(form);queue=rows;
    const next=await resetCompletedForm(form,{refresh:false});if(next)revealBookingForm(next);return;
   }
  }
  if(form.id==='workflowBooking'&&error?.name==='QuotaExceededError')throw Error('Недостаточно места для сохранения брони. Форма остаётся заполненной. Освободите место на устройстве и повторите.');
  throw error;
 }
 form.dataset.pendingKeys=JSON.stringify([...reusedKeys,...items.map(row=>row.key)]);captureForm(form);
 queue=await outboxRows();const list=document.querySelector('#workflowQueue');if(list)list.innerHTML=queueView();updateFormProgress();
 if(form.id==='workflowBooking'){const next=await resetCompletedForm(form,{refresh:false});if(next)revealBookingForm(next);}
}
function bookingFormData(form){
 const f=new FormData(form);
 return {client_name:f.get('client_name'),client_phone:guestPhone(f.get('client_phone')),hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit:f.get('deposit'),shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null};
}
function revealBookingForm(form){
 const section=form.closest('details');if(section)section.open=true;
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
  const actor=data?.actorId,keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=await outboxRows();
  const savedBooking=form.id==='workflowBooking'&&locallySavedBooking(keys.map(k=>rows.find(r=>r.key===k)),actor);
  if(!keys.length||(!savedBooking&&!keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced'))))throw new Error('Сначала завершите текущую отправку.');
  const id=form.id;
  const bookingDefaults=id==='workflowBooking'?Object.fromEntries(['hotel_id','package_id','shoot_date'].map(key=>[key,form.elements[key].value])):null;
  if(refresh&&!await loadWorkflow())return null;
  if(!form.isConnected||actor!==data?.actorId)return null;
  queue=rows;
  clearForm(form);form.reset();if(isInline(selected)&&id==='workflowSale')selected='inline:'+crypto.randomUUID();form.remove();
  captureForms();
  document.querySelector('#app').innerHTML=workflowView({includeBooking:id==='workflowBooking'||!quickBooking});restoreForms();updateFormProgress();
  const next=document.querySelector('#'+id);
  if(next&&bookingDefaults){
   for(const key of ['hotel_id','package_id'])if([...next.elements[key].options].some(option=>option.value===bookingDefaults[key]))next.elements[key].value=bookingDefaults[key];
   if(/^\d{4}-\d{2}-\d{2}$/.test(bookingDefaults.shoot_date)&&bookingDefaults.shoot_date>=todayKey())next.elements.shoot_date.value=bookingDefaults.shoot_date;
   const price=next.querySelector('#bookingFixedPrice');if(price)price.textContent=bookingPriceNote(data,Number(next.elements.hotel_id.value));
   captureForm(next);
  }
  if(next&&quickBooking&&id==='workflowBooking')mountQuickBooking(next);
  return next;
 }finally{
  for(const [el,disabled] of controls)if(el.isConnected)el.disabled=disabled;
  if(form.isConnected){delete form.dataset.resetting;form.removeAttribute('aria-busy');}
 }
}
function mountBookingPhone(form){
 const input=form.elements.client_phone;if(!input||input.dataset.phoneValidation)return;
 input.dataset.phoneValidation='1';input.autocomplete='tel';input.inputMode='tel';
 const status=document.createElement('p');status.className='quick-phone-feedback';status.setAttribute('role','status');input.closest('label').after(status);
 const validate=()=>{try{guestPhone(input.value);input.setCustomValidity('');const count=phoneMatches(input.value,data?.bookings||[]);status.textContent=count?`В доступных записях этот телефон встречается ${count} раз. Проверьте имя гостя.`:'';status.classList.toggle('is-error',false);}catch(error){input.setCustomValidity(error.message);status.textContent=error.message;status.classList.toggle('is-error',true);}};
 input.addEventListener('input',validate);validate();
}
export function closeWorkflowBooking({navigate=false}={}){
 const current=quickBooking;if(!current)return;quickBooking=null;
 current.dialog.removeEventListener('close',current.closed);
 const form=current.dialog.querySelector('#workflowBooking');
 if(form){
  captureForm(form);let section=document.querySelector('#bookingHistory')?.closest('details');
  if(!section&&document.querySelector('#workflowQueue')){section=document.createElement('details');section.className='panel section';section.innerHTML='<summary>Новая бронь</summary><div id="bookingHistory" aria-label="Сохранённые брони">'+bookingHistoryView()+'</div>';document.querySelector('#app').prepend(section);}
  if(section)section.append(form);
 }
 if(current.dialog.open)current.dialog.close();current.dialog.remove();
 if(navigate)current.onClose?.();
}
function mountQuickBooking(form,options={}){
 if(quickBooking&&quickBooking.actor!==data?.actorId)closeWorkflowBooking();
 if(!quickBooking){
  const dialog=document.createElement('dialog');dialog.className='quick-booking-dialog';dialog.setAttribute('aria-labelledby','quickBookingTitle');
  dialog.innerHTML='<header class="quick-booking-head"><h2 id="quickBookingTitle">Новая бронь</h2><button type="button" class="icon-button" data-quick-booking-close aria-label="Закрыть новую бронь">×</button></header><div data-quick-booking-body></div><section class="quick-booking-history" aria-label="Сохранённые брони"><div data-quick-booking-history></div></section>';
  const closed=()=>closeWorkflowBooking({navigate:true});quickBooking={dialog,closed,actor:data?.actorId,onClose:options.onClose};
  dialog.addEventListener('close',closed);dialog.querySelector('[data-quick-booking-close]').addEventListener('click',()=>dialog.close());document.body.append(dialog);
 }
 if(!form.dataset.compactBooking){
  form.dataset.compactBooking='1';form.classList.add('quick-booking-form');
  const basic=new Set(['client_name','client_phone','hotel_id','room','guest_count','shoot_date','shoot_time']);
  const extras=document.createElement('details');extras.className='quick-booking-extra';extras.innerHTML='<summary>Чек и дополнительные параметры</summary>';
  for(const child of [...form.children]){
   if(child.matches('label.field')){const name=child.querySelector('input,select,textarea')?.name;if(!basic.has(name))extras.append(child);else if(['room','guest_count','shoot_date','shoot_time'].includes(name))child.classList.add('quick-field-half');}
   else if(child.id!=='bookingFixedPrice'&&!child.matches('button,[role="status"],[role="alert"],[data-operation-progress]')&&!child.querySelector('[data-operation-progress]'))extras.append(child);
  }
  const button=form.querySelector('button[type="submit"],button:not([type])');
  const deposit=document.createElement('div');deposit.className='quick-booking-deposit';deposit.innerHTML='<strong>Предоплата за бронь <span data-quick-deposit></span></strong><small>Чек можно добавить позже</small>';
  form.insertBefore(deposit,button);form.insertBefore(extras,button);
  const refreshDeposit=()=>{deposit.querySelector('[data-quick-deposit]').textContent=roubles(form.elements.deposit?.value);};
  form.elements.deposit?.addEventListener('input',refreshDeposit);refreshDeposit();
  form.addEventListener('invalid',event=>{if(extras.contains(event.target))extras.open=true;},true);
 }
 mountBookingPhone(form);
 quickBooking.dialog.querySelector('[data-quick-booking-body]').append(form);
 quickBooking.dialog.querySelector('[data-quick-booking-history]').innerHTML=bookingHistoryView();
 if(!quickBooking.dialog.open)quickBooking.dialog.showModal();
}
window.addEventListener('hashchange',()=>closeWorkflowBooking());
export async function openWorkflowBooking(options={}){
 let form=document.querySelector('#workflowBooking');
 if(!form)throw new Error('Создание записи недоступно. Проверьте рабочую роль и доступ к отелю.');
 if(form.dataset.pendingKeys){
  const keys=JSON.parse(form.dataset.pendingKeys),rows=await outboxRows();
  if(keys.length&&(locallySavedBooking(keys.map(k=>rows.find(r=>r.key===k)),data?.actorId)||keys.every(k=>rows.some(r=>r.key===k&&r.status==='synced'))))form=await resetCompletedForm(form,{refresh:false});
  else if(rejectedBooking(keys.map(k=>rows.find(r=>r.key===k)))){form.dataset.repairingBooking='1';queue=rows;updateFormProgress();}
 }
 if(!form)return;
 mountQuickBooking(form,options);
 revealBookingForm(form);
 if(form.dataset.pendingKeys&&form.dataset.repairingBooking!=='1')throw new Error('Предыдущая запись ещё отправляется. Проверьте её результат и очередь ниже; повторно заполнять её не нужно.');
}
export async function workflowClick(button){
 const kind=button.dataset.workflow;
 if(kind==='replace-receipt-files'){const input=button.parentElement.querySelector('input[data-receipt-input]');await replaceOperationFile(input.dataset.workflowReplace,receiptFiles(input));return;}
 if(kind==='receipt-retry'){await api(`/receipts/${Number(button.dataset.receipt)}/retry`,{method:'POST',body:{}});if(await loadWorkflow(selected,true)){document.querySelector('#app').innerHTML=workflowView();restoreForms();updateFormProgress();}return;}
 if(kind==='correct-queued-booking')return correctQueuedBooking(button.dataset.key);
 if(kind==='gallery-recovery'){
  const form=button.closest('#workflowSale'),context=selectionContext();
  if(!form)throw Error('Откройте исходную продажу.');
  button.disabled=true;
  try{
   const id=currentBooking(form)?.id,fresh=await api('/workflow?booking='+id,{allowSnapshot:false});
   if(!selectionCurrent(context)||!form.isConnected||fresh.actorId!==context.actor)throw Error('Аккаунт изменился.');
   data=paymentMode(fresh);const b=currentBooking(form);
   if(!b?.draft?.mine||b.draft.status!=='AWAITING_SELECTED')throw Error('Сначала дождитесь сохранения чека и количества кадров.');
   form.dataset.galleryRecovery='1';const radio=form.querySelector('[name=photoSource][value=gallery]');
   if(!radio)throw Error('Для этой съёмки нет папки оригиналов.');radio.checked=true;
   await openGallerySale(form);updateFormProgress();
  }finally{if(button.isConnected)button.disabled=false;}
  return;
 }

 if(kind==='receipt-review'){
  const context=selectionContext();
  return openReceiptReview({actorId:data?.actorId,roles:data?.canAuditDisk?['OWNER']:[],isCurrent:()=>selectionCurrent(context)&&data?.canAuditDisk===true,onDecision:async()=>{
   if(!selectionCurrent(context))return;
   captureForms();if(await loadWorkflow(selected,true)){document.querySelector('#app').innerHTML=workflowView();restoreForms();window.dispatchEvent(new Event('pb-workflow-mounted'));}
  }},Number(button.dataset.receiptId)||null);
 }
 if(kind==='bank-open'){
  const context=selectionContext(),host=button.closest('[data-auto-bank]');button.disabled=true;
  const stillCurrent=()=>button.isConnected!==false&&selectionCurrent(context);
  try{
   const rows=await outboxRows();if(!stillCurrent())return;
   const row=rows.find(item=>item.key===button.dataset.key);
   if(!row||row.userId!==context.actor||!['booking_bank_payment','sale_bank_payment'].includes(row.kind)||row.error||row.status==='error'||!row.result)throw new Error('Проверьте актуальное состояние банковского счёта.');
   await openBankPaymentLink(row.result,{bookingId:row.result.bookingId,isCurrent:stillCurrent,onChecked:bill=>{
    if(!bankPaymentLink(bill)&&stillCurrent()){clearAutomaticQR();if(host)host.innerHTML=gateView({...row,result:bill});}
   }});
  }catch(error){if(stillCurrent()){clearAutomaticQR();host?.querySelector('[data-workflow="bank-open"]')?.remove();const box=host?.querySelector('[data-auto-bank-qr]');if(box)box.textContent=error.message;}}
  finally{if(button.isConnected!==false)button.disabled=false;}
  return;
 }
 if(kind==='merchant-qr'||kind==='booking-payment'){
  if(receiptsOnly())throw new Error('Прикрепите чек. Поступление подтверждает владелец.');
  const form=button.closest('form'),context=selectionContext();
  const purpose=button.dataset.purpose||'DEPOSIT';
  if(form&&submittingForms.has(form))throw new Error('Дождитесь сохранения текущей продажи.');
  const stillCurrent=()=>button.isConnected!==false&&selectionCurrent(context)&&(!form||form.isConnected!==false);
  button.disabled=true;
  try{
   const prepared=purpose==='PURCHASE'&&form?.id==='workflowSale'&&isInline(form.dataset.draftScope)?await prepareMerchantInline(form,stillCurrent):null;
   if(!stillCurrent())return;
   const id=Number(button.dataset.booking||prepared?.result?.bookingId||(form?.id==='workflowSale'?saleFormBooking(form).id:0));
   if(!Number.isSafeInteger(id)||id<1)throw new Error('Сначала сохраните запись гостя.');
   const fresh=await api('/workflow?booking='+id,{allowSnapshot:false});if(!stillCurrent())return;
   if(prepared){if(fresh.actorId!==context.actor)throw new Error('Аккаунт изменился. Откройте приложение заново.');data=paymentMode(fresh);}
   const b=fresh.bookings.find(row=>row.id===id);if(!b)throw new Error('Запись больше недоступна.');
   if(fresh.bankConfigured){
    if(purpose!=='DEPOSIT')throw new Error('Выберите оплату по QR Сбера в форме продажи.');
    if(context.actor!=null&&context.actor!==fresh.actorId)throw new Error('Аккаунт изменился. Откройте запись заново.');
    await showBookingBankPayment(b,fresh,{isCurrent:stillCurrent});return;
   }
   if(!fresh.merchantQRConfigured)throw new Error('QR для оплаты ещё не настроен.');
   let payload={bookingId:id,total:0,sold:0,tariffId:null,amount:null,requestKey:crypto.randomUUID(),purpose};
   if(purpose==='PURCHASE'){
    if(form?.id!=='workflowSale')throw new Error('Откройте текущую продажу.');
    const rows=await outboxRows();if(!stillCurrent())return;
    payload=bankPaymentPayload(form,b,fresh,{purpose,amount:null,requestKey:payload.requestKey},rows);
   }
   await showMerchantQR(payload,{isCurrent:stillCurrent});
  }finally{if(button.isConnected!==false)button.disabled=false;}
  return;
 }
 if(kind==='repair-inline-sale'){
  const form=button.closest('#workflowSale'),keys=JSON.parse(form?.dataset.pendingKeys||'[]'),rows=await outboxRows();
  if(!form||!rejectedInlineSale(keys.map(key=>rows.find(row=>row.key===key))))throw new Error('Исправление недоступно. Проверьте прежний результат.');
  const context=selectionContext();if(!await loadWorkflow()||!selectionCurrent(context)||!form.isConnected)return;
  form.dataset.repairingInline='1';refreshInlineFields(form);updateFormProgress();return;
 }
 if(kind==='another-sale'){captureForms();workflowSelect('');document.querySelector('#workflowSalePick')?.focus();return;}
 if(['gallery-status','gallery-sync','gallery-qr','copy-gallery'].includes(kind)){
  const form=button.closest('#workflowSale');if(!form)return;
  const b=saleFormBooking(form),rows=JSON.parse(form.dataset.pendingKeys||'[]').map(key=>queue.find(row=>row.key===key));
  if(kind==='gallery-status'){
   form.dataset.galleryPollUntil=String(Date.now()+180000);
   if(await loadWorkflow(b.id,true))updateFormProgress();return;
  }
  if(kind==='gallery-sync'){
   const state=currentSaleState(rows,b),path=String(state.gallery?.syncPath||'').replace(/^\/api\/miniapp(?=\/)/,'');
   if(!state.accepted||path!==`/sales/${state.result.saleId}/gallery/sync`)throw new Error('Сначала дождитесь подтверждения этой продажи сервером.');
   const context=selectionContext(),message=form.querySelector('[data-sale-gallery-message]');button.disabled=true;
   try{
    const result=await api(path,{method:'POST',body:{},allowSnapshot:false});
    if(!selectionCurrent(context)||!form.isConnected)return;
    if(result.gallery?.saleId!==state.result.saleId)throw new Error('Ответ проверки относится к другой продаже. Обновите состояние.');
    const saved=data.bookings.find(row=>row.id===b.id);if(saved)saved.gallery=result.gallery;else b.gallery=result.gallery;updateFormProgress();
   }catch(error){if(message?.isConnected)message.textContent=error.message;throw error;}
   finally{if(button.isConnected)button.disabled=false;}
   return;
  }
  const state=currentSaleState(rows,b);if(!state.ready)throw new Error('Галерея ещё не готова. Дождитесь подтверждения сервера.');
  const box=form.querySelector('[data-sale-gallery]'),message=box?.querySelector('[data-sale-gallery-message]');
  if(kind==='copy-gallery'){await navigator.clipboard.writeText(state.gallery.clientUrl);if(message)message.textContent='Ссылка скопирована.';return;}
  const path=String(state.gallery.qrPath||'').replace(/^\/api\/miniapp(?=\/)/,'');
  if(!/^\/(?:sales\/\d+\/gallery-qr|delivery\/\d+\/qr)$/.test(path))throw new Error('QR-код ещё не подтверждён сервером.');
  button.disabled=true;
  try{
   const blob=await api(path,{responseType:'blob',allowSnapshot:false});
   if(!form.isConnected||(b.scope||b.id)!==selected)return;
   const old=saleQRs.get(state.result.saleId);if(old)URL.revokeObjectURL(old);
   const url=URL.createObjectURL(blob);saleQRs.set(state.result.saleId,url);
   const target=box.querySelector('[data-sale-gallery-qr]');target.innerHTML='';
   const image=document.createElement('img');image.src=url;image.alt='QR-код готовой галереи гостя';image.className='sale-gallery-qr';target.append(image);
  }finally{if(button.isConnected)button.disabled=false;}
  return;
 }
 if(kind==='payment-qr'){
  queue=await outboxRows();const row=queue.find(item=>item.key===button.dataset.key);
  await showAutomaticQR(row,button.closest('[data-auto-bank]')?.querySelector('[data-auto-bank-qr]'));return;
 }
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
  const booking=saleFormBooking(form).id;if(!await loadWorkflow(booking,true)||form.isConnected===false)return;const b=saleFormBooking(form),d=b?.draft,keys=JSON.parse(form.dataset.pendingKeys||'[]'),rows=keys.map(key=>queue.find(row=>row.key===key));
  const rejected=rejectedSaleCounts(rows);
  if(!d?.mine||!(rejected?['AWAITING_COUNTS','AWAITING_SELECTED'].includes(d.status):d.status==='AWAITING_SELECTED')||!saleQueueRepairable(rows))throw new Error('Сначала дождитесь подтверждения всех файлов или исправьте ошибку отправки в очереди.');
  form.dataset.repairing='1';form.elements.total.value=frameTotal(b)||d.total||'';form.elements.total.readOnly=Boolean(b.fullUploaded);form.elements.sold.value=(rejected?rejected.data.sold:d.sold)||'';form.elements.selected.value='';
  const summary=document.querySelector('#workflowSelectedSaleSummary');if(summary)summary.innerHTML=salesHistory(b)+draftProgress(b);
  const details=form.querySelector('.sale-details');if(details)details.open=true;
  updateFormProgress();const target=form.elements[rejected?'total':'sold'];if(target.type!=='hidden')target.focus();return;
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
export function workflowSelect(id){if(!data||document.querySelector('#workflowSale')?.dataset.resetting==='1')return;selectionVersion++;loadVersion++;clearSaleQRs();captureForms();selected=String(id)==='-1'?'inline:'+crypto.randomUUID():isInline(id)?id:Number(id)||null;try{sessionStorage.setItem('pb-workflow-sale:'+data.actorId,String(selected||''));}catch{}document.querySelector('#app').innerHTML=workflowView();mountDiscountPanel();restoreForms();updateFormProgress();syncSaleSelection(document.querySelector('#workflowSale'));refreshQuote();}
function refreshConnection(){const el=document.querySelector('#workflowNotice');if(el)el.textContent=connectionNote();}
window.addEventListener('online',refreshConnection);window.addEventListener('offline',refreshConnection);
let saleRefreshTimer=null,saleRefreshVersion=0,saleRefreshDue=0,lastSaleRefreshAt=0;
const pendingReceiptRefresh=()=>data?.receiptAutomatic===true&&data.bookings.some(b=>(b.receipts||[]).some(r=>r.status==='PENDING'&&![ 'waiting_configuration','retry_exhausted','archived','superseded' ].includes(r.automatic?.state)));
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
   const booking=selected,context=selectionContext(),load=loadVersion,next=await api('/workflow'+(Number(booking)>0?'?booking='+booking:''));
   if(version!==saleRefreshVersion||actor!==data?.actorId||next.actorId!==actor)return;
   if(!selectionCurrent(context)||load!==loadVersion)return;
   if(booking!==selected){scheduleSaleRefresh();return;}
   data=paymentMode(next);
   const overview=document.querySelector('#workflowSalesSummary');if(overview)overview.innerHTML=salesOverview();
   const summary=document.querySelector('#workflowSelectedSaleSummary'),b=data.bookings.find(b=>b.id===selected);
   if(summary)summary.innerHTML=b?salesHistory(b)+draftProgress(b):'';
   updateFormProgress();
   if(pendingReceiptRefresh())scheduleSaleRefresh();
  }catch{if(pendingReceiptRefresh())scheduleSaleRefresh();}
 },delay);
}
window.addEventListener('pb-outbox',async()=>{refreshConnection();const el=document.querySelector('#workflowQueue');if(el){
 const ticket=++queueRefreshVersion,actor=data?.actorId,previous=new Map(queue.map(row=>[row.key,row.status])),rows=await outboxRows();
 if(ticket!==queueRefreshVersion||actor!==data?.actorId)return;queue=rows;
 el.innerHTML=queueView();refreshBookingHistory();updateFormProgress();
 const accepted=queue.filter(row=>row.kind.startsWith('sale_')&&row.status==='synced'&&previous.get(row.key)!=='synced');
 if(accepted.length)scheduleSaleRefresh(accepted.some(row=>row.kind==='sale_complete'));
}});

export function bookingAccepted(rows){return Boolean(rows[0]?.kind==='booking'&&rows[0]?.status==='synced'&&Number.isSafeInteger(rows[0]?.result?.bookingId)&&rows[0].result.bookingId>0);}
function updateFormProgress(){
 for(const form of document.querySelectorAll('#workflowBooking,#workflowSale,#workflowShoot')){
  recoverSaleQueue(form);
  if(!form.dataset.pendingKeys)continue;
  let keys;try{keys=JSON.parse(form.dataset.pendingKeys);}catch{continue;}
  const rows=keys.map(key=>queue.find(row=>row.key===key)),done=rows.filter(r=>r?.status==='synced').length;
  const last=rows.find(row=>row?.kind==='sale_complete')||rows[rows.length-1],allFinished=done===keys.length&&keys.length>0;
  const b=form.id==='workflowSale'?currentBooking(form):null;
  const saleState=form.id==='workflowSale'?currentSaleState(rows,b):null;
  const finished=form.id==='workflowSale'?saleState.accepted:allFinished||(form.id==='workflowBooking'&&bookingAccepted(rows));
  let note=form.querySelector('[data-operation-progress]');if(!note){note=document.createElement('div');note.dataset.operationProgress='';note.setAttribute('role','status');form.append(note);}
  const failed=rows.find(r=>r?.autoRetryPaused&&r.status==='error')||rows.find(r=>r?.error&&r.status!=='syncing');
  const repairable=form.id==='workflowSale'&&saleQueueRepairable(rows),repairing=repairable&&form.dataset.repairing==='1',rejected=repairable&&rejectedSaleCounts(rows);
  const inlineRepairable=form.id==='workflowSale'&&rejectedInlineSale(rows),inlineRepairing=inlineRepairable&&form.dataset.repairingInline==='1';
  const bookingRepairable=form.id==='workflowBooking'&&rejectedBooking(rows),bookingRepairing=bookingRepairable&&form.dataset.repairingBooking==='1';
  note.textContent=bookingRepairing?`Запись не была создана: ${bookingRepairable.error} Исправьте поля и сохраните повторно. Прежний чек остаётся в очереди.`:bookingRepairable?`Запись не была создана: ${bookingRepairable.error} Нажмите «Исправить запись», чтобы изменить данные этой же отправки.`:repairing?(rejected?'Общее количество сверено с завершённой полной съёмкой. Сохраните исправление: прежний чек, проданное количество и выбранные файлы остаются в очереди и продолжат отправку.':'Исправьте фактическое количество проданных кадров или добавьте недостающие новые фотографии. Принятые чек и фото сохранятся. Повторные фотографии не увеличивают количество.'):finished?(form.id==='workflowBooking'&&rows[0]?.result?.bookingId?`Запись №${rows[0].result.bookingId} сохранена сервером.`:last?.result?.saleId?`Продажа №${last.result.saleId} сохранена сервером: ${last.result.soldPhotos??form.elements.sold?.value??'—'} фото на ${roubles(last.result.amount)}. Подтверждение оплаты показывается отдельно.`:'Операции приняты сервером. Для оригиналов отдельно проверьте статус на Яндекс.Диске.'):`В очереди сохранены все данные. Сервер принял ${done} из ${keys.length}. ${failed?.error||'Отправка продолжается.'} Повторное заполнение не требуется.`;
  if(finished&&!allFinished&&form.id==='workflowBooking')note.textContent+=rows.some(row=>row?.kind==='booking_bank_payment')?' Оплата по QR ожидает подтверждения банка. Счёт остаётся в очереди ниже.':' Чек предоплаты ещё не принят. Он остаётся в очереди ниже; можно создать следующую запись.';
  const gate=rows.find(row=>['booking_bank_payment','sale_bank_payment'].includes(row?.kind));
  let payment=form.querySelector('[data-form-bank-payment]');
  if(gate?.result){if(!payment){payment=document.createElement('div');payment.dataset.formBankPayment='';form.append(payment);}payment.innerHTML=gateView(gate,true);}else payment?.remove();
  if(form.id==='workflowBooking'&&bookingAccepted(rows)&&data.merchantQRConfigured&&!data.bankConfigured&&Number(rows[0].data?.deposit)>0){
   let qr=form.querySelector('[data-booking-merchant-qr]');
   if(!qr){qr=document.createElement('button');qr.type='button';qr.className='btn primary';qr.dataset.bookingMerchantQr='';qr.dataset.workflow='merchant-qr';qr.dataset.purpose='DEPOSIT';qr.textContent='Показать QR предоплаты гостю';form.append(qr);}
   qr.dataset.booking=String(rows[0].result.bookingId);
  }
  if(form.id==='workflowSale'){
   if(!repairing&&!inlineRepairable){
    note.textContent=saleState.accepted?`Продажа №${saleState.result.saleId} сохранена сервером. ${saleState.ready?'Все фотографии подтверждены; галерея готова.':'Фотографии сохранены; готовность гостевой галереи показана ниже. Подтверждение оплаты — отдельно.'}`:`Сохраняем продажу. Передано фотографий: ${saleState.transferred} из ${saleState.files}. ${failed?.error||'Отправка идёт автоматически.'} Повторно заполнять форму не нужно.`;
   }
   let progress=form.querySelector('[data-sale-photo-progress]');
   if(!progress){progress=document.createElement('p');progress.dataset.salePhotoProgress='';progress.setAttribute('role','status');note.append(progress);}
   progress.textContent=saleTransferProgress(rows);
   if(inlineRepairable)note.textContent=inlineRepairing?'Исправьте данные нового гостя. Фотографии и чек сохранены; эта же отправка продолжится после сохранения.':`Новая продажа не создана: ${inlineRepairable.error} Исправьте данные; фотографии и чек сохранены на устройстве.`;
   saleGalleryView(form,saleState);
   if(saleState.accepted&&!saleState.ready){
    if(!form.dataset.galleryPollUntil)form.dataset.galleryPollUntil=String(Date.now()+180000);
    if(Date.now()<Number(form.dataset.galleryPollUntil))scheduleSaleRefresh();
   }
  }
  for(const el of [...form.elements])if(!['button','submit'].includes(el.type))el.disabled=true;
  if(inlineRepairing)for(const el of [...form.elements])if(el.name&&el.type!=='file'&&!['sold','total'].includes(el.name))el.disabled=false;
  if(bookingRepairing)for(const el of [...form.elements])if(el.name&&el.type!=='file')el.disabled=false;
  if(repairing)for(const name of rejected?['total']:['total','sold','selected'])form.elements[name].disabled=false;
  if(repairing&&!rejected)for(const el of form.querySelectorAll('[data-sale-disk] input,[data-sale-disk] button,[data-sale-disk] fieldset'))el.disabled=false;
  if(form.id==='workflowSale'&&form.dataset.galleryRecovery==='1'){
   for(const control of form.querySelectorAll('[data-sale-gallery] input,[data-sale-gallery] button'))control.disabled=false;
  }
  if(form.id==='workflowSale'&&rows.some(row=>row?.kind==='sale_disk_selected'&&canReplaceDiskSelection(row))){
   for(const el of form.querySelectorAll('[data-sale-disk] input,[data-sale-disk] button,[data-sale-disk] fieldset'))el.disabled=false;
   note.textContent+=' Папка Диска изменилась: выберите один новый кадр в форме продажи и нажмите «Заменить отклонённый кадр» у ошибки в очереди. Чек и принятые фото сохранятся.';
  }
  const button=form.querySelector('button[type="submit"],button:not([type])');
  if(button){button.disabled=finished||(form.id==='workflowSale'&&!failed&&!repairing)||(inlineRepairable&&!inlineRepairing);button.textContent=inlineRepairing?'Сохранить исправление и продолжить':bookingRepairing?'Сохранить исправленную запись':repairing?(rejected?'Сохранить общее количество и продолжить':'Сохранить исправление и завершить'):finished?(form.id==='workflowSale'?'Продажа сохранена':'Сохранено'):failed?(failed.autoRetryPaused?'Продолжить отправку':'Повторить сохранённую отправку'):form.id==='workflowSale'?'Сохранение продолжается':'Проверить отправку';}
  if(form.id==='workflowSale'&&form.dataset.galleryRecovery==='1'&&button){button.disabled=false;button.textContent='Продолжить продажу из галереи';}
  let galleryRecovery=form.querySelector('[data-workflow="gallery-recovery"]');
  const canRecoverGallery=form.id==='workflowSale'&&b?.draft?.mine&&b.draft.status==='AWAITING_SELECTED'&&rows.some(row=>row?.kind==='sale_selected'&&row.status!=='synced'&&row.error&&!row.supersededByGallery)&&!rows.some(row=>row?.status==='syncing');
  if(canRecoverGallery&&!galleryRecovery){galleryRecovery=document.createElement('button');galleryRecovery.type='button';galleryRecovery.className='btn ghost';galleryRecovery.dataset.workflow='gallery-recovery';galleryRecovery.textContent='Продолжить из галереи без повторной загрузки';form.append(galleryRecovery);}
  if(galleryRecovery)galleryRecovery.hidden=!canRecoverGallery||form.dataset.galleryRecovery==='1';
  let inlineRepair=form.querySelector('[data-workflow="repair-inline-sale"]');
  if(inlineRepairable&&!inlineRepair){inlineRepair=document.createElement('button');inlineRepair.type='button';inlineRepair.className='btn ghost';inlineRepair.dataset.workflow='repair-inline-sale';inlineRepair.textContent='Исправить данные нового гостя';form.append(inlineRepair);}
  if(inlineRepair)inlineRepair.hidden=!inlineRepairable||inlineRepairing;
  let bookingRepair=form.querySelector('[data-workflow="repair-booking"]');
  if(bookingRepairable&&!bookingRepair){bookingRepair=document.createElement('button');bookingRepair.type='button';bookingRepair.className='btn ghost';bookingRepair.dataset.workflow='repair-booking';bookingRepair.textContent='Исправить запись';form.append(bookingRepair);}
  if(bookingRepair)bookingRepair.hidden=!bookingRepairable||bookingRepairing;
  let repair=form.querySelector('[data-workflow="repair-sale"]');
  if(repairable&&!repair){repair=document.createElement('button');repair.type='button';repair.className='btn ghost';repair.dataset.workflow='repair-sale';repair.textContent='Исправить количество / добавить недостающие фото';form.append(repair);}
  if(repair)repair.textContent=rejected?'Сверить общее количество и продолжить отправку':'Исправить количество / добавить недостающие фото';
  if(repair)repair.hidden=!repairable||repairing;
  if((finished||form.id==='workflowBooking'&&locallySavedBooking(rows,data?.actorId))&&!form.querySelector('[data-workflow="new-operation"]')){const next=document.createElement('button');next.type='button';next.className='btn ghost';next.dataset.workflow='new-operation';next.textContent=form.id==='workflowBooking'?'Создать следующую запись':'Начать новую операцию';form.append(next);}
 }
 mountAutomaticQR();
}
document.addEventListener('pb-gallery-selection',event=>{syncSaleSelection(event.target.closest?.('#workflowSale'));});
window.addEventListener('pb-workflow-mounted',()=>{mountBookingPhone(document.querySelector('#workflowBooking')||{elements:{}});refreshInlineFields(document.querySelector('#workflowSale'));updateFormProgress();syncSaleSelection(document.querySelector('#workflowSale'));void restoreCommittedBooking();if(pendingReceiptRefresh())scheduleSaleRefresh();});
document.addEventListener('pb-workflow-submit-finished',updateFormProgress);
document.addEventListener('change',async event=>{
 const input=event.target;if(!input?.matches?.('[data-workflow-replace]')||input.hasAttribute('data-receipt-input')||!input.files?.length)return;
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

function refreshInlineFields(form){
 if(!form||!isInline(form.dataset.draftScope)||form.dataset.pendingKeys&&form.dataset.repairingInline!=='1')return;
 const b=currentBooking(form),cfg=data.inlineSale||{},people=(cfg.photographers||[]).filter(row=>row.hotelIds.includes(b.hotelId)),picker=form.elements.photographer_id;
 if(picker?.tagName==='SELECT'){
  const old=picker.value;picker.innerHTML='<option value="">Выберите фотографа</option>'+options(people);
  if(people.some(row=>String(row.id)===old))picker.value=old;
 }
 const controls=form.querySelector('[data-sale-pricing-controls]');
 if(controls)controls.innerHTML=tariffOptions(b,data.tariffs).replace(/<div class="notice" id="salePricePreview"[\s\S]*$/,'');
 let packageField=form.querySelector('[data-inline-package]');
 const ambiguous=!b.price||data.hotels.find(row=>row.id===b.hotelId)?.unitPrice==null&&new Set(data.packages.map(row=>String(row.price))).size>1;
 if(ambiguous&&!form.elements.package_id){packageField=document.createElement('label');packageField.className='field';packageField.dataset.inlinePackage='';packageField.innerHTML='Тариф<select class="input" name="package_id" required><option value="">Выберите тариф</option>'+data.packages.map(row=>`<option value="${row.id}">${esc(row.name)} · ${esc(row.price)} ₽/кадр</option>`).join('')+'</select>';form.elements.guest_name.closest('label').after(packageField);}
 if(!ambiguous&&packageField)packageField.remove();
 captureForm(form);refreshQuote();
}
function refreshQuote(){const form=document.querySelector('#workflowSale'),b=currentBooking(form);updatePricePreview(form,b,data?.tariffs);}
document.addEventListener('input',event=>{if(event.target.closest('#workflowSale'))refreshQuote();});
document.addEventListener('change',event=>{if(event.target.form?.id==='workflowSale'&&['hotel_id','package_id'].includes(event.target.name))refreshInlineFields(event.target.form);if(event.target.closest('#workflowSale')||event.target.name==='discountId')refreshQuote();if(event.target.name==='selected'||event.target.name==='photoSource'||event.target.matches?.('[data-disk-photo]'))syncSaleSelection(event.target.closest('#workflowSale'));});
window.addEventListener('pb-workflow-mounted',()=>{mountDiscountPanel();refreshQuote();});

window.addEventListener('pb-bank-paid',async event=>{if(event.detail?.bookingId&&Number(event.detail.bookingId)!==selected)return;if(await loadWorkflow(selected))refreshQuote();});

document.addEventListener('change',event=>{if(event.target?.name==='hotel_id'&&event.target.form?.id==='workflowBooking'){const note=document.querySelector('#bookingFixedPrice');if(note)note.textContent=bookingPriceNote(data,Number(event.target.value));}});

function invalidateWorkflowView(){loadVersion++;selectionVersion++;saleRefreshVersion++;clearSaleQRs();if(saleRefreshTimer)clearTimeout(saleRefreshTimer);saleRefreshTimer=null;}
window.addEventListener('hashchange',invalidateWorkflowView);
window.addEventListener('pagehide',invalidateWorkflowView);
function resetWorkflow(){invalidateWorkflowView();closeBookingCorrection();data=null;queue=[];selected=null;clearComparisons();clearReceiptViewer();}
document.addEventListener('pb-chat-account-reset',resetWorkflow);
window.addEventListener('pb-operations-reset',resetWorkflow);
