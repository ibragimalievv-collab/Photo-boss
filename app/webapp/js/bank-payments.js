import {api} from './api.js';
import {esc} from './domain.js';
import {updatePricePreview} from './hotel-pricing.js';
import {outboxRows} from './outbox.js';
let booking=null,settings=null,qrURL=null,generation=0,qrVersion=0;
function clearQR(){qrVersion++;if(qrURL)URL.revokeObjectURL(qrURL);qrURL=null;}
function contextFor(element){return {generation,bookingId:booking?.id,panel:element.closest('#bankPanel')};}
function current(context){return context.generation===generation&&context.bookingId===booking?.id&&context.panel&&context.panel===document.querySelector('#bankPanel');}
const states={CREATING:'Создание не подтверждено — проверьте статус',PENDING:'Ожидает оплаты',PAID:'Поступление подтверждено банком',TEST_PAID:'Тестовая оплата — деньги в кассу не зачислены',DECLINED:'Отклонён банком',CANCELLED:'Отменён',REFUNDED:'Возвращён',PARTIAL_REFUND:'Частичный возврат'};
export function bankPanel(b,data){generation++;clearQR();booking=b;settings=data;if(!b)return '';const balance=Boolean(b.sales?.length&&!b.draft);return `<section id="bankPanel" class="panel section"><h3>Оплата гостем · СберБизнес</h3><p>${data.bankConfigured?'Счёт привязан к этой съёмке и отелю. QR откроет платёжную страницу банка; доступные способы оплаты определяет эквайринг Сбера.':'СберБизнес ещё не подключён. Для рабочих QR владелец должен подключить интернет-эквайринг и добавить настройки магазина на сервере.'}</p><form id="bankPaymentForm" class="form-grid"><label class="field">Что оплачивает гость<select class="input" name="purpose"><option value="PURCHASE">Покупку по количеству и тарифу в форме выше</option><option value="DEPOSIT">Заявленную предоплату по записи</option><option value="BALANCE" ${balance?'selected':''}>Остаток уже оформленных продаж</option></select></label><label class="field">Сумма сейчас, ₽ (пусто — весь остаток)<input class="input" name="amount" type="number" min="0.01" max="10000000" step="0.01"></label><button class="btn primary" ${data.bankConfigured?'':'disabled'}>Сформировать платёжный QR</button><p role="status"></p></form><div id="bankBills">${(b.bankPayments||[]).map(billView).join('')}</div><div id="bankQR"></div></section>`;}
export function bankPaymentPayload(sale,b,data,{purpose,amount,requestKey},rows=[]){
 let keys=[];try{keys=JSON.parse(sale?.dataset.pendingKeys||'[]');}catch{throw new Error('Не удалось определить текущую продажу. Обновите её состояние перед оплатой.');}
 const operations=rows.filter(row=>keys.includes(row.key)),sourceDraftId=operations.find(row=>row.result?.draftId)?.result.draftId||b.draft?.id||null;
 if(purpose==='PURCHASE'&&operations.some(row=>row.status==='synced'&&Number.isSafeInteger(row.result?.saleId)))purpose='BALANCE';
 const quote=purpose==='PURCHASE'?updatePricePreview(sale,b,data.tariffs):null;
 if(purpose==='PURCHASE'&&!quote)throw new Error('Сначала выберите съёмку и заполните количество кадров.');
 return {bookingId:b.id,total:purpose==='PURCHASE'?Number(sale.elements.total.value):0,
  sold:purpose==='PURCHASE'?Number(sale.elements.sold.value):0,tariffId:quote?.tariffId||null,
  ...(sourceDraftId?{sourceDraftId}:{}),discountId:quote?.discountId||null,albumId:quote?.albumId||null,
  albumQuantity:quote?.albumQuantity??null,amount:amount||null,purpose,requestKey};
}
function billView(bill){return `<article class="section" data-bank-bill="${bill.id}"><strong>Счёт №${bill.id}: ${esc(bill.amount)} ₽</strong><p>${bill.testMode?'ТЕСТОВАЯ СРЕДА · ':''}${esc(states[bill.status]||bill.status)}</p>${bill.status==='PENDING'&&bill.qrPath?`<button type="button" class="btn ghost" data-bank-qr="${bill.id}">Показать QR</button><a class="btn ghost" href="${esc(bill.url)}" target="_blank" rel="noopener noreferrer">Открыть оплату</a>`:''}<button type="button" class="btn ghost" data-bank-check="${bill.id}">Проверить в банке</button><p role="status"></p></article>`;}
async function showQR(id,context){const ticket=++qrVersion,blob=await api(`/bank-payments/${id}/qr`,{responseType:'blob'});if(ticket!==qrVersion||!current(context))return;clearQR();qrURL=URL.createObjectURL(blob);const box=context.panel.querySelector('#bankQR');box.innerHTML='';const image=document.createElement('img');image.src=qrURL;image.alt='QR платёжной страницы СберБизнес';image.style.maxWidth='320px';box.append(image);}
function invalidate(){generation++;clearQR();}
window.addEventListener('hashchange',invalidate);
window.addEventListener('pb-operations-reset',invalidate);
document.addEventListener('pb-chat-account-reset',invalidate);
document.addEventListener('submit',async event=>{const form=event.target;if(form.id!=='bankPaymentForm')return;event.preventDefault();const context=contextFor(form);if(!current(context))return;const button=form.querySelector('button');if(button.disabled)return;button.disabled=true;try{
 const next=await api('/workflow?booking='+context.bookingId,{allowSnapshot:false});if(!current(context))return;
 const refreshed=next.bookings.find(row=>row.id===context.bookingId);
 if(!refreshed)throw new Error('Съёмка больше недоступна. Обновите рабочие операции.');
 const rows=await outboxRows();if(!current(context))return;
 booking=refreshed;settings=next;const sale=document.querySelector('#workflowSale'),fields=new FormData(form);
 const payload=form._pendingBankPayload||bankPaymentPayload(sale,booking,settings,{purpose:fields.get('purpose'),amount:fields.get('amount'),requestKey:form.dataset.requestKey||crypto.randomUUID()},rows);
 form.dataset.requestKey=payload.requestKey;form._pendingBankPayload=payload;
 const bill=await api('/bank-payments',{method:'POST',body:payload});
 delete form.dataset.requestKey;delete form._pendingBankPayload;if(!current(context))return;
 const existing=context.panel.querySelector(`[data-bank-bill="${bill.id}"]`);
 if(existing)existing.outerHTML=billView(bill);else context.panel.querySelector('#bankBills').insertAdjacentHTML('beforeend',billView(bill));
 if(bill.qrPath&&bill.status==='PENDING')await showQR(bill.id,context);
 if(current(context))form.querySelector('[role=status]').textContent='Счёт сохранён. Оформляйте продажу после подтверждения банка или загрузки чека.';
 }catch(error){if(error.status&&error.status<500){delete form.dataset.requestKey;delete form._pendingBankPayload;}if(current(context))form.querySelector('[role=status]').textContent=error.message;}finally{button.disabled=false;}});
document.addEventListener('click',async event=>{const button=event.target.closest('[data-bank-qr],[data-bank-check]');if(!button||button.disabled)return;const context=contextFor(button);if(!current(context))return;button.disabled=true;try{
 if(button.dataset.bankQr)await showQR(Number(button.dataset.bankQr),context);
 else{
  const bill=await api(`/bank-payments/${button.dataset.bankCheck}/status`,{method:'POST',body:{}});if(!current(context))return;
  button.closest('[data-bank-bill]').outerHTML=billView(bill);
  if(bill.status==='PAID'){
   const next=await api('/workflow?booking='+context.bookingId,{allowSnapshot:false});if(!current(context))return;
   const refreshed=next.bookings.find(b=>b.id===context.bookingId);if(!refreshed)throw new Error('Съёмка больше недоступна. Обновите рабочие операции.');
   booking=refreshed;settings=next;updatePricePreview(document.querySelector('#workflowSale'),booking,next.tariffs);
   context.panel.querySelector('#bankQR').textContent='Банк подтвердил поступление. Можно сохранить продажу без фото чека.';window.dispatchEvent(new Event('pb-bank-paid'));
  }
 }
 }catch(error){if(current(context)){const el=context.panel.querySelector(`[data-bank-bill="${button.dataset.bankCheck||button.dataset.bankQr}"] [role=status]`)||context.panel.querySelector('#bankPaymentForm [role=status]');if(el)el.textContent=error.message;}}finally{button.disabled=false;}});
