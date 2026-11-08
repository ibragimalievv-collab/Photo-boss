import {enqueueOperation,outboxRows} from './outbox.js';
import {todayKey} from './domain.js';
import {bindReceiptInput,receiptFiles,validateReceiptFiles} from './receipt-files.js';

let active=null;
export function openBookingReceipt(bookingId,{isCurrent=()=>true,onSaved=()=>{}}={}){
 if(!Number.isSafeInteger(bookingId)||bookingId<1||!isCurrent())throw Error('Откройте запись заново.');
 active?.remove();
 const dialog=document.createElement('dialog');active=dialog;
 const automatic=window.PHOTO_BOSS_CONFIG?.RECEIPT_AUTOMATIC===true;
 dialog.className='sheet';
 dialog.innerHTML='<form class="form-grid"><h2>Чек предоплаты</h2><p>Бронь — 1 000 ₽. После съёмки учтённая предоплата вычитается из покупки фотографий.</p><label class="field">Фото или PDF чека<input class="input" name="receipt" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" required></label><p>'+(automatic?'Фотобосс автоматически проверит сумму, получателя, дату и статус в чеке. Подтверждение владельцем не требуется. Ответ банка учитывается отдельно.':'Фото само по себе не подтверждает оплату. Владелец проверит поступление по банковским данным.')+'</p><button class="btn primary" type="submit">Отправить на проверку</button><p role="status"></p><button class="btn ghost" type="button" data-close>Закрыть</button></form>';
 dialog.querySelector('[data-close]').onclick=()=>dialog.close();
 dialog.addEventListener('close',()=>{if(active===dialog)active=null;dialog.remove();},{once:true});
 dialog.querySelector('form').onsubmit=async event=>{
  event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]'),message=form.querySelector('[role=status]');
  if(button.disabled)return;button.disabled=true;
  try{
   const file=validateReceiptFiles(receiptFiles(form.elements.receipt));
   const rows=await outboxRows();if(!isCurrent()||active!==dialog)throw Error('Аккаунт изменился. Откройте запись заново.');
   if(rows.some(row=>row.kind==='booking_receipt'&&row.data?.booking?.id===bookingId&&row.status!=='synced'))throw Error('Чек этой записи уже находится в очереди. Проверьте его отправку в разделе «Работа».');
   await enqueueOperation('booking_receipt',todayKey(),{booking:{id:bookingId},purpose:'DEPOSIT'},file);
   if(!isCurrent()||active!==dialog)return;
   form.elements.receipt.disabled=true;message.textContent=automatic?'Чек сохранён в очередь отправки и автоматической проверки. Результат появится в разделе «Работа».':'Чек в очереди отправки. Поступление ещё не подтверждено. Ход отправки доступен в разделе «Работа».';onSaved();
  }catch(error){if(active===dialog){message.textContent=error.message;button.disabled=false;}}
 };
 document.body.append(dialog);bindReceiptInput(dialog.querySelector('input[name=receipt]'));dialog.showModal();
}
document.addEventListener('pb-chat-account-reset',()=>active?.close());
