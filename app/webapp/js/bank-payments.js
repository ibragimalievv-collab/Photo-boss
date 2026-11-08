import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {updatePricePreview} from './hotel-pricing.js';
import {outboxRows,enqueueBatch,retryOperation,flushOutbox} from './outbox.js';

let booking=null,settings=null,qrURL=null,qrBillId=null,generation=0,qrVersion=0,poller=null,knownBills=new Map(),connectionVersion=0;
const checks=new Map(),announcedPayments=new Set();
const POLL_INTERVAL=10000,MAX_POLL_CHECKS=120,MAX_POLL_AGE=20*60*1000;
const pendingStatuses=new Set(['CREATING','PENDING']);
const receiptsOnly=()=>typeof window!=='undefined'&&window.PHOTO_BOSS_CONFIG?.PAYMENT_MODE==='receipts';
const states={CREATING:'Создание не подтверждено — проверьте статус',PENDING:'Ожидает оплаты',PAID:'Поступление подтверждено банком',TEST_PAID:'Тестовая оплата — деньги в кассу не зачислены',DECLINED:'Отклонён банком',CANCELLED:'Отменён',REFUNDED:'Возвращён',PARTIAL_REFUND:'Частичный возврат',RECONCILIATION_REQUIRED:'Банк подтвердил денежную операцию. Второе поступление не добавлено; нужна сверка владельца. Новый QR создавать нельзя.'};

export function bankConnectionPanel(data){
 connectionVersion++;
 if(data?.paymentMode==='receipts'||receiptsOnly())return '';
 if(!data?.canCheckBankConnection)return '';
 return `<section id="bankConnectionPanel" class="panel section"><h3>Подключение Сбера</h3><p>Проверка сертификата и доступа к банку без выставления счёта гостю.</p><button type="button" class="btn ghost" data-bank-connection>Проверить подключение Сбера</button><p role="status" aria-live="polite">Проверка ещё не выполнялась.</p></section>`;
}
function connectionText(result){
 const environment=result.environment==='production'?'Рабочая среда':'Тестовая среда';
 const certificate=result.certificate?.checked?(result.certificate.valid===true?'Сертификат проверен.':'Сертификат не прошёл проверку.'):'Сертификат ещё не проверен.';
 const access=result.oauth?.authenticated===true?'Сбер подтвердил доступ приложения.':'Доступ приложения банком пока не подтверждён.';
 const readiness=result.orderConfigurationComplete===true?'Настройки для QR заполнены.':'Для QR ещё нужны подтверждённые параметры банка.';
 return `${environment}. ${certificate} ${access} ${readiness} ${result.enabled===true?'Оплата гостем этой проверкой не подтверждается.':'Приём платежей через QR.API выключен.'}`;
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-bank-connection]');if(!button||button.disabled)return;
 const panel=button.closest('#bankConnectionPanel'),version=connectionVersion,current=()=>version===connectionVersion&&panel===document.querySelector('#bankConnectionPanel');
 if(!panel)return;const status=panel.querySelector('[role=status]');button.disabled=true;status.textContent='Проверяем настройки подключения…';
 try{
  let result=await api('/bank-payments/connection',{allowSnapshot:false});if(!current())return;
  status.textContent=connectionText(result);
  if(result.authenticationConfigurationComplete===true){result=await api('/bank-payments/connection/check',{method:'POST',body:{},timeoutMs:30000});if(current())status.textContent=connectionText(result);}
 }catch(error){if(current())status.textContent+=' '+error.message;}
 finally{if(current())button.disabled=false;}
});

const paymentHosts={sber_qr:new Set(['qr.nspk.ru','qr.sberbank.ru']),sber_ecom:new Set(['ecommerce.sberbank.ru','ecomtest.sberbank.ru','ecomift.sberbank.ru','securepayments.sberbank.ru','sbergate.com','payecom.ru'])};
export function bankPaymentLink(bill){
 if(receiptsOnly())return null;
 if(bill?.status!=='PENDING'||!Number.isSafeInteger(bill.id)||bill.id<1||typeof bill.testMode!=='boolean'||typeof bill.amount!=='string'||!/^\d+(?:\.\d{1,2})?$/.test(bill.amount)||!Number.isFinite(Number(bill.amount))||Number(bill.amount)<=0)return null;
 const value=bill.url;if(typeof value!=='string'||!value.startsWith('https://')||/[\s\u0000-\u001f\u007f\\]/.test(value))return null;
 try{
  const url=new URL(value),authority=value.slice(value.indexOf('//')+2).split(/[/?#]/)[0];
  if(url.protocol!=='https:'||authority.includes('@')||url.username||url.password||url.port&&url.port!=='443'||url.hash||!paymentHosts[bill.provider]?.has(url.hostname))return null;
  if(bill.provider==='sber_qr'&&(value.length>256||!url.pathname||url.pathname==='/'))return null;
  return url.href;
 }catch{return null;}
}
export function bankPaymentStatus(bill){return states[bill?.status]||(bill?.status==='RECONCILIATION_REQUIRED'?'Банк подтвердил денежную операцию; нужна сверка владельца. Новый QR создавать нельзя.':'Подтверждение оплаты не получено.');}
export async function openBankPaymentLink(bill,{bookingId,isCurrent,onChecked}={}){
 if(typeof isCurrent!=='function'||!isCurrent())return null;
 if(!Number.isSafeInteger(bill?.id)||bill.id<1||!Number.isSafeInteger(bookingId)||bookingId<1)throw new Error('Не удалось определить банковский счёт.');
 const checked=await api(`/bank-payments/${bill.id}/status`,{method:'POST',body:{},allowSnapshot:false,timeoutMs:20000});
 if(!isCurrent())return null;
 if(checked?.id!==bill.id||checked.bookingId!==bookingId)throw new Error('Счёт банка не совпал с выбранной записью.');
 if(onChecked)await onChecked(checked);
 if(!isCurrent())return null;
 if(checked.status!=='PENDING')return checked;
 const url=bankPaymentLink(checked);if(!url)throw new Error('Банк не подтвердил безопасную ссылку оплаты. Проверьте счёт.');
 location.assign(url);return checked;
}
function clearQR(){qrVersion++;if(qrURL)URL.revokeObjectURL(qrURL);qrURL=null;qrBillId=null;}
function route(){return typeof location==='undefined'?null:location.hash;}
function contextFor(element){return {generation,bookingId:booking?.id,actorId:settings?.actorId,route:route(),panel:element.closest('#bankPanel')};}
function current(context){return context.generation===generation&&context.bookingId===booking?.id&&context.actorId===settings?.actorId&&context.route===route()&&context.panel&&context.panel===document.querySelector('#bankPanel');}
function pageAvailable(){return !document.hidden&&(typeof navigator==='undefined'||navigator.onLine!==false);}
function pausePolling(){if(poller?.timer!=null&&typeof clearTimeout==='function')clearTimeout(poller.timer);if(poller)poller.timer=null;}
function stopPolling(){pausePolling();if(poller)poller.stopped=true;poller=null;}
function pollingCurrent(state){return state===poller&&!state.stopped&&state.generation===generation&&state.bookingId===booking?.id&&state.actorId===settings?.actorId;}
function pollingContext(state){return {generation:state.generation,bookingId:state.bookingId,actorId:state.actorId,route:state.route,panel:document.querySelector('#bankPanel')};}
function pollMessage(context,message,id=null){if(!current(context))return;const target=(id&&context.panel.querySelector(`[data-bank-bill="${id}"] [role=status]`))||context.panel.querySelector('#bankPaymentForm [role=status]');if(target)target.textContent=message;}
function schedulePoll(state,delay=POLL_INTERVAL){
 if(!pollingCurrent(state)||state.running||state.timer!=null||!state.pending.size||!pageAvailable()||typeof setTimeout!=='function')return;
 if(state.checkCount>=MAX_POLL_CHECKS||Date.now()-state.startedAt>=MAX_POLL_AGE){state.stopped=true;pollMessage(pollingContext(state),'Автоматическая проверка завершена. При необходимости нажмите «Проверить в банке».');return;}
 state.timer=setTimeout(()=>{state.timer=null;return pollPending(state);},delay);
}
function resumePolling(){if(poller)schedulePoll(poller,0);}
function preparePolling(b,data){
 stopPolling();announcedPayments.clear();
 if(!b||!data.bankConfigured||data.paymentMode==='receipts'||receiptsOnly())return;
 poller={generation,bookingId:b.id,actorId:data.actorId,route:route(),pending:new Map((b.bankPayments||[]).filter(bill=>pendingStatuses.has(bill.status)).map(bill=>[bill.id,bill])),timer:null,running:false,stopped:false,startedAt:Date.now(),checkCount:0,failures:0};
 schedulePoll(poller,0);
}
function rememberBill(bill){
 knownBills.set(bill.id,bill);
 if(!poller||!pollingCurrent(poller))return;
 if(pendingStatuses.has(bill.status))poller.pending.set(bill.id,bill);else poller.pending.delete(bill.id);
 if(!poller.pending.size)pausePolling();
}
function retryPaymentRefresh(bill,context){if(poller&&pollingCurrent(poller)&&poller.generation===context.generation&&poller.bookingId===context.bookingId)poller.pending.set(bill.id,bill);}
function paymentPanelText(data){
 const qr=data.bankProvider==='sber_qr',test=data.bankTestMode===true;
 if(!data.bankConfigured)return qr?'Сбер QR.API ещё не готов к оплате. Владелец должен завершить настройку доступа банка и сертификата.':'СберБизнес ещё не подключён. Для рабочих QR владелец должен подключить интернет-эквайринг и добавить настройки магазина на сервере.';
 if(qr)return (test?'Тестовая среда Сбера: реальные поступления не подтверждаются. ':'')+'QR формируется банком для суммы этой съёмки. Статус оплаты проверяется автоматически, пока панель открыта.';
 return (test?'Тестовая среда Сбера: реальные поступления не подтверждаются. ':'')+'Счёт привязан к этой съёмке и отелю. QR откроет платёжную страницу банка; доступные способы оплаты определяет эквайринг Сбера. Статус оплаты проверяется автоматически, пока панель открыта.';
}
export function bankPanel(b,data){
 generation++;clearQR();booking=b;settings=data;knownBills=new Map((b?.bankPayments||[]).map(bill=>[bill.id,bill]));preparePolling(b,data);if(!b)return '';
 if(data.paymentMode==='receipts'||receiptsOnly())return '<section class="notice"><h3>Оплата по чеку</h3><p>Прикрепите чек к продаже. Владелец сверит фактическое поступление и подтвердит сумму. До полной оплаты фотографии гостю недоступны.</p></section>';
 const balance=Boolean(b.sales?.length&&!b.draft);
 return `<section id="bankPanel" class="panel section"><h3>Оплата гостем · СберБизнес</h3><p>${paymentPanelText(data)}</p><form id="bankPaymentForm" class="form-grid"><label class="field">Что оплачивает гость<select class="input" name="purpose"><option value="PURCHASE">Покупку по количеству и тарифу в форме выше</option><option value="DEPOSIT">Заявленную предоплату по записи</option><option value="BALANCE" ${balance?'selected':''}>Остаток уже оформленных продаж</option></select></label><label class="field">Сумма сейчас, ₽ (пусто — весь остаток)<input class="input" name="amount" type="number" min="0.01" max="10000000" step="0.01"></label><button class="btn primary" ${data.bankConfigured?'':'disabled'}>Сформировать платёжный QR</button><p role="status"></p></form><div id="bankBills">${(b.bankPayments||[]).map(billView).join('')}</div><div id="bankQR"></div></section>`;
}
export function bankPaymentPayload(sale,b,data,{purpose,amount,requestKey},rows=[]){
 let keys=[];try{keys=JSON.parse(sale?.dataset.pendingKeys||'[]');}catch{throw new Error('Не удалось определить текущую продажу. Обновите её состояние перед оплатой.');}
 const operations=rows.filter(row=>keys.includes(row.key)),sourceDraftId=operations.find(row=>row.result?.draftId)?.result.draftId||b.draft?.id||null;
 if(purpose==='PURCHASE'&&operations.some(row=>row.status==='synced'&&Number.isSafeInteger(row.result?.saleId)))purpose='BALANCE';
 const quote=purpose==='PURCHASE'?updatePricePreview(sale,b,data.tariffs):null;
 if(purpose==='PURCHASE'&&!quote)throw new Error('Сначала выберите съёмку и заполните количество кадров.');
 return {bookingId:b.id,total:purpose==='PURCHASE'?Number(sale.elements.total.value):0,
  sold:purpose==='PURCHASE'?Number(sale.elements.sold.value):0,tariffId:quote?.tariffId||null,
  ...(sourceDraftId?{sourceDraftId}:{}),discountId:quote?.discountId||null,albumId:quote?.albumId||null,
  albumQuantity:quote?.albumQuantity??null,...(quote?.photoAmount!=null?{photoAmount:quote.photoAmount}:{}),amount:amount||null,purpose,requestKey};
}
function billView(bill){return `<article class="section" data-bank-bill="${bill.id}"><strong>Счёт №${bill.id}: ${esc(bill.amount)} ₽</strong><p>${bill.testMode?'ТЕСТОВАЯ СРЕДА · ':''}${esc(bankPaymentStatus(bill))}</p>${bankPaymentLink(bill)&&bill.qrPath===`/bank-payments/${bill.id}/qr`?`<button type="button" class="btn ghost" data-bank-qr="${bill.id}">Показать QR</button><button type="button" class="btn ghost" data-bank-open="${bill.id}">Оплатить на этом телефоне</button>`:''}<button type="button" class="btn ghost" data-bank-check="${bill.id}">Проверить в банке</button><p role="status"></p></article>`;}
async function showQR(id,context){
 const ticket=++qrVersion,blob=await api(`/bank-payments/${id}/qr`,{responseType:'blob'});if(ticket!==qrVersion||!current(context))return;
 clearQR();qrURL=URL.createObjectURL(blob);qrBillId=id;const box=context.panel.querySelector('#bankQR');box.innerHTML='';
 const provider=knownBills.get(id)?.provider||settings?.bankProvider;
 const image=document.createElement('img');image.src=qrURL;image.alt=(provider==='sber_qr'?'Платёжный QR Сбера':'QR платёжной страницы СберБизнес');image.style.maxWidth='320px';box.append(image);
}
function validateBill(bill,id,context){if(!bill||!Number.isSafeInteger(bill.id)||bill.id<1||bill.id!==id||(bill.bookingId!=null&&bill.bookingId!==context.bookingId))throw new Error('Счёт банка не совпал с выбранной съёмкой. Обновите её состояние.');}
async function readBill(id,context){
 const key=`${context.generation}:${id}`;
 if(checks.has(key))return checks.get(key);
 const request=api(`/bank-payments/${id}/status`,{method:'POST',body:{},timeoutMs:20000});checks.set(key,request);
 try{return await request;}finally{if(checks.get(key)===request)checks.delete(key);}
}
async function acceptBill(bill,context,row=null){
 if(!current(context))return;
 validateBill(bill,bill.id,context);rememberBill(bill);
 const article=context.panel.querySelector(`[data-bank-bill="${bill.id}"]`)||row;if(article)article.outerHTML=billView(bill);
 const test=bill.testMode===true||(bill.testMode==null&&settings?.bankTestMode===true);
 if(bill.status==='TEST_PAID'||(bill.status==='PAID'&&test)){
  if(qrBillId==null||qrBillId===bill.id){clearQR();context.panel.querySelector('#bankQR').textContent='Тестовая оплата получена. Реальные поступления и продажа не подтверждены.';}
  return;
 }
 if(!pendingStatuses.has(bill.status)&&bill.status!=='PAID'&&qrBillId===bill.id){clearQR();context.panel.querySelector('#bankQR').textContent=states[bill.status]||bill.status;}
 const announcementKey=`${context.generation}:${bill.id}`;
 if(bill.status!=='PAID'||test||announcedPayments.has(announcementKey))return;
 announcedPayments.add(announcementKey);
 try{
  const next=await api('/workflow?booking='+context.bookingId,{allowSnapshot:false});if(!current(context))return;
  if(context.automatic&&!pageAvailable()){announcedPayments.delete(announcementKey);retryPaymentRefresh(bill,context);return;}
  const refreshed=next.bookings.find(b=>b.id===context.bookingId);if(!refreshed)throw new Error('Съёмка больше недоступна. Обновите рабочие операции.');
  booking=refreshed;settings=next;updatePricePreview(document.querySelector('#workflowSale'),booking,next.tariffs);
  if(qrBillId==null||qrBillId===bill.id){clearQR();context.panel.querySelector('#bankQR').textContent='Банк подтвердил поступление. Можно сохранить продажу без фото чека.';}
  const event=typeof CustomEvent==='function'?new CustomEvent('pb-bank-paid',{detail:{bookingId:context.bookingId,billId:bill.id}}):new Event('pb-bank-paid');
  window.dispatchEvent(event);
 }catch(error){announcedPayments.delete(announcementKey);retryPaymentRefresh(bill,context);throw error;}
}
async function pollPending(state){
 if(!pollingCurrent(state)||state.running||!pageAvailable())return;
 const context={...pollingContext(state),automatic:true};if(!current(context))return;
 if(state.checkCount>=MAX_POLL_CHECKS||Date.now()-state.startedAt>=MAX_POLL_AGE){state.stopped=true;pollMessage(context,'Автоматическая проверка завершена. При необходимости нажмите «Проверить в банке».');return;}
 state.running=true;
 try{
  for(const id of [...state.pending.keys()]){
   if(!pollingCurrent(state)||!current(context)||!pageAvailable()||state.checkCount>=MAX_POLL_CHECKS||Date.now()-state.startedAt>=MAX_POLL_AGE)break;
   state.checkCount++;
   try{
    const bill=await readBill(id,context);if(!pollingCurrent(state)||!current(context)||!pageAvailable())return;
    validateBill(bill,id,context);await acceptBill(bill,context);state.failures=0;
   }catch(error){
    if(!pollingCurrent(state)||!current(context)||!pageAvailable())return;
    pollMessage(context,error.message||'Проверка банка временно недоступна. Повторим её автоматически.',id);
    if([401,403].includes(error.status)){state.stopped=true;return;}
    if([404,409].includes(error.status)){state.pending.delete(id);continue;}
    state.failures++;break;
   }
  }
 }finally{state.running=false;if(pollingCurrent(state))schedulePoll(state,Math.min(60000,POLL_INTERVAL*2**state.failures));}
}
function invalidate(){generation++;connectionVersion++;stopPolling();clearQR();announcedPayments.clear();clearBookingBankPayment();}
window.addEventListener('hashchange',invalidate);
window.addEventListener('pagehide',invalidate);
window.addEventListener('pb-operations-reset',invalidate);
document.addEventListener('pb-chat-account-reset',invalidate);
window.addEventListener('offline',pausePolling);
window.addEventListener('online',resumePolling);
window.addEventListener('pb-workflow-mounted',()=>{resumePolling();if(activeBookingQR&&!bookingQRCurrent(activeBookingQR))clearBookingBankPayment();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)pausePolling();else resumePolling();});

document.addEventListener('submit',async event=>{
 const form=event.target;if(form.id!=='bankPaymentForm')return;event.preventDefault();const context=contextFor(form);if(!current(context))return;
 const button=form.querySelector('button');if(button.disabled)return;button.disabled=true;
 try{
  const next=await api('/workflow?booking='+context.bookingId,{allowSnapshot:false});if(!current(context))return;
  const refreshed=next.bookings.find(row=>row.id===context.bookingId);if(!refreshed)throw new Error('Съёмка больше недоступна. Обновите рабочие операции.');
  const rows=await outboxRows();if(!current(context))return;
  booking=refreshed;settings=next;const sale=document.querySelector('#workflowSale'),fields=new FormData(form);
  const payload=form._pendingBankPayload||bankPaymentPayload(sale,booking,settings,{purpose:fields.get('purpose'),amount:fields.get('amount'),requestKey:form.dataset.requestKey||crypto.randomUUID()},rows);
  form.dataset.requestKey=payload.requestKey;form._pendingBankPayload=payload;
  const bill=await api('/bank-payments',{method:'POST',body:payload});
  delete form.dataset.requestKey;delete form._pendingBankPayload;if(!current(context))return;
  validateBill(bill,bill.id,context);const existing=context.panel.querySelector(`[data-bank-bill="${bill.id}"]`);
  if(existing)existing.outerHTML=billView(bill);else context.panel.querySelector('#bankBills').insertAdjacentHTML('beforeend',billView(bill));
  rememberBill(bill);resumePolling();if(bill.qrPath&&bill.status==='PENDING')await showQR(bill.id,context);
  if(current(context))form.querySelector('[role=status]').textContent='Счёт сохранён. Оформляйте продажу после подтверждения банка или загрузки чека.';
 }catch(error){if(error.status&&error.status<500){delete form.dataset.requestKey;delete form._pendingBankPayload;}if(current(context))form.querySelector('[role=status]').textContent=error.message;}
 finally{button.disabled=false;}
});

document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-bank-qr],[data-bank-check],[data-bank-open]');if(!button||button.disabled)return;
 const context=contextFor(button);if(!current(context))return;button.disabled=true;
 try{
  if(button.dataset.bankOpen)await openBankPaymentLink({id:Number(button.dataset.bankOpen)},{bookingId:context.bookingId,isCurrent:()=>current(context),onChecked:bill=>acceptBill(bill,context,button.closest('[data-bank-bill]'))});
  else if(button.dataset.bankQr)await showQR(Number(button.dataset.bankQr),context);
  else{const id=Number(button.dataset.bankCheck),bill=await readBill(id,context);if(!current(context))return;validateBill(bill,id,context);await acceptBill(bill,context,button.closest('[data-bank-bill]'));resumePolling();}
 }catch(error){if(current(context)&&button.dataset.bankOpen){button.remove?.();clearQR();context.panel.querySelector('#bankQR').textContent=error.message;}pollMessage(context,error.message,button.dataset.bankCheck||button.dataset.bankQr||button.dataset.bankOpen);}
 finally{button.disabled=false;}
});

// Presentation of the existing durable outbox gate; no second bank-create path.

let bookingQRGeneration=0,activeBookingQR=null;
const bookingQROpenings=new Map(),unresolvedBookingBills=new Set(['CREATING','PENDING','RECONCILIATION_REQUIRED']);
const bookingQRCurrent=context=>activeBookingQR===context&&context.bookingQRGeneration===bookingQRGeneration&&context.route===route()&&context.dialog.isConnected&&context.isCurrent();
const bookingQRNode=(tag,text)=>{const value=document.createElement(tag);if(text!=null)value.textContent=text;return value;};
function clearBookingQRImage(context){if(context.imageURL)URL.revokeObjectURL(context.imageURL);context.imageURL=null;context.imageVersion++;context.controls.replaceChildren();}
export function clearBookingBankPayment(){const previous=activeBookingQR;activeBookingQR=null;bookingQRGeneration++;if(previous){clearBookingQRImage(previous);if(previous.dialog.open)previous.dialog.close();previous.dialog.remove();}}

async function bookingGate(booking,settings,isCurrent){
 const identity=`${settings.actorId}:${booking.id}`;
 if(bookingQROpenings.has(identity)){
  const pendingOpen=bookingQROpenings.get(identity),saved=await pendingOpen;
  if(saved||!isCurrent())return saved;
  if(bookingQROpenings.get(identity)===pendingOpen)bookingQROpenings.delete(identity);
  return bookingGate(booking,settings,isCurrent);
 }
 const task=(async()=>{
  const rows=await outboxRows();if(!isCurrent())return null;
  const existing=(booking.bankPayments||[]).find(bill=>unresolvedBookingBills.has(bill.status));
  const sameBooking=row=>row.bankPayload?.bookingId===booking.id||row.data?.booking?.id===booking.id||row.result?.bookingId===booking.id;
  let saved=existing?rows.find(row=>['booking_bank_payment','sale_bank_payment'].includes(row.kind)&&sameBooking(row)&&(row.result?.id===existing.id||row.data?.existingBillId===existing.id)):null;
  if(!existing)saved=rows.find(row=>row.kind==='booking_bank_payment'&&sameBooking(row)&&row.status!=='synced'&&(!row.result||unresolvedBookingBills.has(row.result.status)));
  if(saved)return saved;
  const [row]=await enqueueBatch([{key:crypto.randomUUID(),kind:'booking_bank_payment',date:todayKey(),
   data:{booking:{id:booking.id},payment:{},...(existing?{existingBillId:existing.id}:{})}}]);
  return row;
 })();bookingQROpenings.set(identity,task);
 try{return await task;}finally{if(bookingQROpenings.get(identity)===task)bookingQROpenings.delete(identity);}
}

async function displayBookingQR(context,row){
 if(!bookingQRCurrent(context))return;
 const bill=row?.result;
 const stamp=JSON.stringify([row?.status,row?.error,bill]);if(stamp===context.stamp)return;context.stamp=stamp;
 clearBookingQRImage(context);
 context.message.textContent=row?.error||(bill?(bill.testMode?'ТЕСТОВАЯ СРЕДА · ':'')+bankPaymentStatus(bill):'Создаём банковский счёт. Повторная заявка не нужна.');
 context.amount.textContent=bill?.amount?'Сумма счёта: '+bill.amount+' ₽':'';
 if(!bill||row.error||row.status==='error'||!bankPaymentLink(bill)||bill.bookingId!==context.bookingId||bill.qrPath!==`/bank-payments/${bill.id}/qr`)return;
 const open=bookingQRNode('button','Оплатить на этом телефоне');open.type='button';open.className='btn primary';
 open.onclick=async()=>{
  if(!bookingQRCurrent(context)||open.disabled)return;open.disabled=true;
  try{await openBankPaymentLink(bill,{bookingId:context.bookingId,isCurrent:()=>bookingQRCurrent(context),onChecked:checked=>displayBookingQR(context,{...row,result:checked})});}
  catch(error){if(bookingQRCurrent(context)){context.stamp=null;clearBookingQRImage(context);context.message.textContent=error.message;}}
  finally{if(bookingQRCurrent(context))open.disabled=false;}
 };
 context.controls.append(open,bookingQRNode('p','На другом телефоне отсканируйте QR в приложении банка.'));
 const version=context.imageVersion;
 try{
  const blob=await api(bill.qrPath,{responseType:'blob',allowSnapshot:false});
  if(!bookingQRCurrent(context)||version!==context.imageVersion)return;
  if(!blob||blob.type!=='image/png'||!Number.isFinite(blob.size)||blob.size<=0||blob.size>2*1024*1024)throw new Error('Банк не подтвердил изображение QR. Проверьте счёт.');
  const image=bookingQRNode('img');context.imageURL=URL.createObjectURL(blob);image.src=context.imageURL;image.alt='Банковский QR для суммы этой записи';image.style.maxWidth='300px';
  image.onerror=()=>{if(bookingQRCurrent(context)&&version===context.imageVersion){context.stamp=null;clearBookingQRImage(context);context.message.textContent='QR не загрузился. Проверьте счёт ещё раз.';}};
  context.controls.append(image);
 }catch(error){if(bookingQRCurrent(context)&&version===context.imageVersion){context.stamp=null;clearBookingQRImage(context);context.message.textContent=error.message;}}
}
async function refreshBookingQR(){
 const context=activeBookingQR;if(!context)return;if(!bookingQRCurrent(context)){clearBookingBankPayment();return;}
 const ticket=++context.refreshVersion;
 try{const rows=await outboxRows();if(bookingQRCurrent(context)&&ticket===context.refreshVersion)await displayBookingQR(context,rows.find(row=>row.key===context.key));}
 catch(error){if(bookingQRCurrent(context)){context.stamp=null;clearBookingQRImage(context);context.message.textContent=error.message;}}
}
export async function showBookingBankPayment(booking,settings,{isCurrent}={}){
 if(receiptsOnly()||settings?.paymentMode==='receipts')throw new Error('Прикрепите чек предоплаты. Поступление подтвердит владелец.');
 clearBookingBankPayment();
 if(!Number.isSafeInteger(booking?.id)||booking.id<1||!Number.isSafeInteger(settings?.actorId)||settings.actorId<1||settings.bankConfigured!==true||typeof isCurrent!=='function')throw new Error('Откройте актуальную запись и проверьте доступ к банку.');
 if(!isCurrent())return;
 if(['0','0.00'].includes(booking.declaredDeposit)&&!(booking.bankPayments||[]).some(bill=>unresolvedBookingBills.has(bill.status)))throw new Error('Предоплата по этой записи не требуется. Новый QR не нужен.');
 const ticket=bookingQRGeneration,row=await bookingGate(booking,settings,()=>ticket===bookingQRGeneration&&isCurrent());
 if(!row||ticket!==bookingQRGeneration||!isCurrent())return;
 const dialog=bookingQRNode('dialog');dialog.className='sheet';dialog.setAttribute('aria-label','Банковская оплата записи');dialog.style.cssText='max-width:420px;width:calc(100vw - 32px);padding:20px;';
 const close=bookingQRNode('button','Закрыть');close.type='button';close.className='btn ghost';close.onclick=clearBookingBankPayment;
 const message=bookingQRNode('p'),amount=bookingQRNode('p'),controls=bookingQRNode('div'),check=bookingQRNode('button','Проверить счёт');message.setAttribute('role','status');
 const context={bookingQRGeneration:ticket,route:route(),bookingId:booking.id,actorId:settings.actorId,isCurrent,dialog,message,amount,controls,key:row.key,imageVersion:0,imageURL:null,refreshVersion:0};
 check.type='button';check.className='btn ghost';check.onclick=async()=>{if(!bookingQRCurrent(context)||check.disabled)return;check.disabled=true;context.stamp=null;try{await retryOperation(context.key);await refreshBookingQR();}catch(error){if(bookingQRCurrent(context)){clearBookingQRImage(context);context.message.textContent=error.message;}}finally{if(bookingQRCurrent(context))check.disabled=false;}};
 dialog.append(close,bookingQRNode('h2','Оплата по банковскому счёту · запись №'+booking.id),amount,message,controls,check);
 activeBookingQR=context;document.body.append(dialog);dialog.addEventListener('close',()=>{if(activeBookingQR===context)clearBookingBankPayment();});dialog.addEventListener('cancel',event=>{event.preventDefault();if(activeBookingQR===context)clearBookingBankPayment();});
 try{dialog.showModal();await displayBookingQR(context,row);void flushOutbox();await refreshBookingQR();}catch(error){if(bookingQRCurrent(context))clearBookingBankPayment();throw error;}
}
window.addEventListener('pb-outbox',()=>void refreshBookingQR());
window.addEventListener('pb-operations-stale',invalidate);
