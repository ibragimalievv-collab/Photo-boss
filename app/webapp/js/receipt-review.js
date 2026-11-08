import {api} from './api.js';
import {esc} from './domain.js';
import {receiptTitle,receiptMessage} from './receipt-state.js';

const native=()=>window.PHOTO_BOSS_CONFIG?.STANDALONE===true;
let active=null,revision=0;
const money=value=>value==null?'не указана':new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB'}).format(Number(value));
const current=context=>Boolean(active===context&&context.revision===revision&&context.dialog.isConnected&&context.isCurrent());
const message=(context,text)=>{if(current(context))context.message.textContent=text;};
const accessLost=(context,error)=>{if([401,403].includes(error?.status)&&current(context)){clearReceiptReview();return true;}return false;};
const element=(tag,text,className)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;if(className)node.className=className;return node;};

export function receiptReviewButton(roles,id=null,source='workflow'){
 if(!native()||!roles?.includes('OWNER'))return '';
 const valid=Number.isSafeInteger(id)&&id>0;
 return `<button type="button" class="btn ghost" data-${source==='app'?'action':'workflow'}="receipt-review"${valid?` data-receipt-id="${id}"`:''}>${window.PHOTO_BOSS_CONFIG?.RECEIPT_AUTOMATIC===true?'Статусы чеков':valid?'Проверить поступление':'Проверить поступления'}</button>`;
}

export function receiptDecisionPayload(action,amount,checked){
 if(action==='reject')return {action,expectedStatus:'PENDING'};
 if(action!=='approve')throw new Error('Выберите решение по чеку.');
 const text=String(amount||'').trim().replace(',','.');
 if(!/^\d{1,8}(?:\.\d{1,2})?$/.test(text)||Number(text)<=0||Number(text)>10000000)
  throw new Error('Укажите положительную сумму из банковской выписки, не более 10 000 000 ₽.');
 if(checked!==true)throw new Error('Сначала сверьте поступление с банковской выпиской.');
 return {action,verifiedAmount:text,bankStatementChecked:true,expectedStatus:'PENDING'};
}

export function clearReceiptReview(){
 revision++;const previous=active;active=null;
 if(previous?.url)URL.revokeObjectURL(previous.url);
 if(previous?.dialog.open)previous.dialog.close();
 previous?.dialog.remove();
}

function parseReceipt(payload){
 const receipt=payload?.receipt||payload;
 if(!Number.isSafeInteger(receipt?.id)||receipt.id<=0||!['PENDING','APPROVED','REJECTED'].includes(receipt.status))
  throw new Error('Сервер не подтвердил состояние чека. Обновите список.');
 return receipt;
}

function totalsView(totals){
 if(!totals)return '';
 const paid=totals.approvedTotal??totals.paidAmount??totals.paid;
 const due=totals.remainingAmount??totals.balanceDue??totals.dueAmount??totals.due;
 return `<p>${paid!=null?`Учтено оплат: <strong>${esc(money(paid))}</strong>. `:''}${due!=null?`Остаток к оплате: <strong>${esc(money(due))}</strong>.`:''}</p>`;
}

async function showImage(context,receipt){
 const imageVersion=context.imageVersion;
 const path=String(context.selectedImage||receipt.imagePath||'').replace(/^\/api\/miniapp(?=\/)/,'');
 const target=context.body.querySelector('[data-review-image]');
 if(!path||!new RegExp('^/receipts/'+receipt.id+'/image(?:\\?file=[12])?$').test(path)){if(target)target.textContent='Фото чека недоступно. Решение принимает владелец после проверки фактического поступления.';return;}
 try{
  const blob=await api(path,{responseType:'blob',allowSnapshot:false});
  if(!current(context)||context.receiptId!==receipt.id||context.imageVersion!==imageVersion)return;
  if(context.url)URL.revokeObjectURL(context.url);
  context.url=URL.createObjectURL(blob);
  const image=element(blob.type==='application/pdf'?'a':'img');
  if(blob.type==='application/pdf'){image.href=context.url;image.target='_blank';image.rel='noopener';image.textContent='Открыть PDF чека';}else{image.src=context.url;image.alt='Загруженный чек №'+receipt.id;}
  image.style.maxWidth='100%';image.style.height='auto';target.replaceChildren(image);
 }catch(error){if(!accessLost(context,error)&&current(context)&&context.receiptId===receipt.id&&target?.isConnected)target.textContent='Фото не загрузилось: '+error.message;}
}

function detailView(context,receipt,totals){
 context.imageVersion++;
 if(context.url){URL.revokeObjectURL(context.url);context.url=null;}
 context.receiptId=receipt.id;context.receipt=receipt;
 const pending=receipt.status==='PENDING'&&!receipt.automatic;
 const status=receiptTitle(receipt);context.selectedImage=null;
 const warnings=Array.isArray(receipt.warnings)?receipt.warnings:[];
 context.body.innerHTML=`<button type="button" class="btn ghost" data-review-queue>К очереди чеков</button>
 <h3>Чек №${receipt.id}${receipt.bookingId?` · запись №${esc(receipt.bookingId)}`:''}</h3>
 <p>${esc(receipt.client||receipt.guestName||'Гость')}${receipt.hotel||receipt.hotelName?` · ${esc(receipt.hotel||receipt.hotelName)}`:''}</p>
 <p>${receipt.purpose==='DEPOSIT'?'Предоплата по записи':'Оплата покупки'} · ${esc(status)}</p>
 <p>Ожидаемая сумма: <strong>${esc(money(receipt.expectedAmount))}</strong>${receipt.verifiedAmount!=null?` · подтверждено: <strong>${esc(money(receipt.verifiedAmount))}</strong>`:''}.</p>
 ${totalsView(totals)}${(receipt.images||[]).map((file,i)=>`<button type="button" class="btn ghost" data-review-attachment="${esc(file.path)}">Вложение ${i+1}</button>`).join('')}<div data-review-image role="status">Загружаем фото чека…</div>
 <p>${esc(receiptMessage(receipt))}</p>
 ${warnings.map(text=>`<p>${esc(text)}</p>`).join('')}${receipt.duplicateApproved?'<p>Похожий платёж уже учтён. Повторное подтверждение запрещено.</p>':''}
 ${pending?`<form data-receipt-decision class="form-grid"><label class="field">Фактически поступило, ₽<input class="input" name="verifiedAmount" inputmode="decimal" autocomplete="off" maxlength="16" placeholder="Сумма из банковской выписки" required></label>
 <label><input type="checkbox" name="bankStatementChecked" required> Я сверил поступление с банковской выпиской</label>
 <button type="submit" class="btn primary" ${receipt.duplicateApproved?'disabled':''}>Подтвердить поступление</button><button type="button" class="btn ghost" data-review-reject>Отклонить чек</button></form>`:'<p>Решение сохранено сервером. Повторное подтверждение не требуется.</p>'}
 <button type="button" class="btn ghost" data-review-refresh>Обновить чек</button>`;
 context.body.querySelector('[data-review-queue]').onclick=()=>loadQueue(context);
 context.body.querySelector('[data-review-refresh]').onclick=()=>loadReceipt(context,receipt.id);
 const form=context.body.querySelector('[data-receipt-decision]');
 if(form){form.onsubmit=event=>{event.preventDefault();decide(context,'approve');};context.body.querySelector('[data-review-reject]').onclick=()=>decide(context,'reject');}
 for(const button of context.body.querySelectorAll('[data-review-attachment]'))button.onclick=()=>{context.imageVersion++;context.selectedImage=button.dataset.reviewAttachment;showImage(context,receipt);};
 showImage(context,receipt);
}

async function loadReceipt(context,id){
 if(!current(context)||context.busy)return;
 const ticket=++context.load;message(context,'Загружаем актуальные данные…');
 try{
  const payload=await api(`/receipts/review/${id}`,{allowSnapshot:false});
  if(!current(context)||ticket!==context.load)return;
  const receipt=parseReceipt(payload);if(receipt.id!==id)throw new Error('Чек изменился. Откройте его из очереди заново.');
  detailView(context,receipt,payload.paymentTotals||receipt.paymentTotals);message(context,'');
 }catch(error){if(!accessLost(context,error))message(context,error.message);}
}

async function loadQueue(context,cursor=null){
 if(!current(context)||context.busy)return;
 const ticket=++context.load;message(context,'Обновляем очередь чеков…');
 try{
  const payload=await api('/receipts/review'+(cursor!=null?'?cursor='+encodeURIComponent(cursor):''),{allowSnapshot:false});
  if(!current(context)||ticket!==context.load)return;
  if(!Array.isArray(payload?.items))throw new Error('Не удалось получить очередь чеков.');
  const rows=payload.items.map(parseReceipt);
  if(context.url){URL.revokeObjectURL(context.url);context.url=null;}
  context.receiptId=null;context.receipt=null;
  context.body.innerHTML=`<p>${window.PHOTO_BOSS_CONFIG?.RECEIPT_AUTOMATIC===true?'Чеки проверяются автоматически; здесь можно увидеть их состояние.':'Здесь только чеки, по которым ожидается решение владельца. Чек черновика появится после завершения продажи.'}</p>
  ${rows.length?rows.map(row=>`<article class="panel panel-pad"><h3>Чек №${row.id} · запись №${esc(row.bookingId||'—')}</h3><p>${esc(row.client||row.guestName||'Гость')}${row.hotel||row.hotelName?` · ${esc(row.hotel||row.hotelName)}`:''}</p><p>${row.purpose==='DEPOSIT'?'Предоплата':'Покупка'} · ожидается ${esc(money(row.expectedAmount))}</p><button type="button" class="btn" data-review-open="${row.id}">Открыть и проверить</button></article>`).join(''):'<p>Чеков на проверку нет.</p>'}
  <button type="button" class="btn ghost" data-review-queue>Обновить очередь</button>${payload.nextCursor!=null?'<button type="button" class="btn ghost" data-review-next>Следующие чеки</button>':''}`;
  for(const button of context.body.querySelectorAll('[data-review-open]'))button.onclick=()=>loadReceipt(context,Number(button.dataset.reviewOpen));
  context.body.querySelector('[data-review-queue]').onclick=()=>loadQueue(context);
  const next=context.body.querySelector('[data-review-next]');if(next)next.onclick=()=>loadQueue(context,payload.nextCursor);
  message(context,'');
 }catch(error){if(!accessLost(context,error))message(context,error.message);}
}

async function decide(context,action){
 if(!current(context)||context.busy||context.receipt?.status!=='PENDING')return;
 if(action==='approve'&&context.receipt.duplicateApproved){message(context,'Этот платёж уже учтён. Повторное подтверждение запрещено.');return;}
 const form=context.body.querySelector('[data-receipt-decision]');if(!form)return;
 let body;
 try{body=receiptDecisionPayload(action,form.elements.verifiedAmount.value,form.elements.bankStatementChecked.checked);}
 catch(error){message(context,error.message);return;}
 context.busy=true;const id=context.receiptId;
 const controls=[...context.body.querySelectorAll('button,input')].map(control=>[control,control.disabled]);controls.forEach(([control])=>control.disabled=true);
 message(context,'Сохраняем решение…');
 let committed=false;
 try{
  const result=await api(`/receipts/review/${id}/decision`,{method:'POST',body,allowSnapshot:false});
  if(!current(context)||context.receiptId!==id)return;
  const receipt=parseReceipt(result);if(receipt.id!==id||receipt.status==='PENDING')throw new Error('Решение пока не подтверждено сервером. Обновите чек.');
  committed=true;
  detailView(context,{...context.receipt,...receipt},result.paymentTotals||receipt.paymentTotals);
  message(context,receipt.status==='APPROVED'?'Поступление подтверждено сервером.':'Отклонение чека сохранено сервером.');
  await context.onDecision?.(result);
 }catch(error){if(!accessLost(context,error))message(context,committed?'Решение сохранено сервером. Обновите раздел, чтобы увидеть актуальные итоги.':error.message+' Введённые данные сохранены. Повторная отправка выполняется только по вашему нажатию.');}
 finally{context.busy=false;if(current(context))for(const [control,disabled] of controls)if(control.isConnected)control.disabled=disabled;}
}

export async function openReceiptReview({actorId,roles,isCurrent,onDecision},receiptId=null){
 if(!native()||!Number.isSafeInteger(actorId)||actorId<=0||!roles?.includes('OWNER')||isCurrent?.()!==true)return;
 clearReceiptReview();
 const dialog=element('dialog',null,'sheet receipt-review-dialog');dialog.setAttribute('aria-label','Проверка поступлений');
 const heading=element('h2','Проверка поступлений'),close=element('button','Закрыть','btn ghost'),body=element('div'),note=element('p');
 close.type='button';note.setAttribute('role','status');note.setAttribute('aria-live','polite');
 const context={actorId,roles,isCurrent,onDecision,dialog,body,message:note,revision,load:0,imageVersion:0,busy:false,url:null,receiptId:null};
 dialog.append(close,heading,body,note);document.body.append(dialog);active=context;
 close.onclick=clearReceiptReview;dialog.addEventListener('close',()=>{if(active===context)clearReceiptReview();});
 dialog.addEventListener('cancel',event=>{event.preventDefault();if(active===context)clearReceiptReview();});
 try{dialog.showModal();}catch(error){clearReceiptReview();throw error;}
 if(Number.isSafeInteger(receiptId)&&receiptId>0)await loadReceipt(context,receiptId);else await loadQueue(context);
}

window.addEventListener('hashchange',clearReceiptReview);
window.addEventListener('pagehide',clearReceiptReview);
window.addEventListener('storage',event=>{if(event.key==='pb-account-changed')clearReceiptReview();});
document.addEventListener('pb-chat-account-reset',clearReceiptReview);
window.addEventListener('pb-operations-reset',clearReceiptReview);
