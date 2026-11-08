import {api} from './api.js';

let generation=0,active=null;
const purposes=new Set(['DEPOSIT','PURCHASE','BALANCE']);
const paymentHosts=new Set(['qr.nspk.ru']);
const route=()=>typeof location==='undefined'?null:location.hash;
const invalid=()=>new Error('Данные оплаты не совпали с выбранной съёмкой. Откройте QR заново.');

function current(context){
 try{return context.generation===generation&&context.route===route()&&context.isCurrent()===true&&(!context.dialog||(active?.dialog===context.dialog&&context.dialog.isConnected));}
 catch{return false;}
}
function discard(context){if(context.dialog&&active?.dialog===context.dialog)clearMerchantQR();}
function unavailable(context,message){
 if(!current(context)){discard(context);return;}
 if(active.url){URL.revokeObjectURL(active.url);active.url=null;}
 context.image?.removeAttribute('src');context.details?.replaceChildren();context.status.textContent=message;
}
function cents(value){
 if(!['string','number'].includes(typeof value)||String(value).trim()==='')throw invalid();
 if(typeof value==='string'&&!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value))throw invalid();
 const amount=Number(value),rounded=Math.round(amount*100);
 if(!Number.isFinite(amount)||amount<0||amount>10000000||Math.abs(amount*100-rounded)>0.000001)throw invalid();
 return rounded;
}
function money(value){return new Intl.NumberFormat('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2}).format(value/100)+' ₽';}
function paymentLink(value){
 if(value==null)return null;
 if(typeof value!=='string'||/[\s\\]/.test(value))throw invalid();
 let url;try{url=new URL(value);}catch{throw invalid();}
 if(url.protocol!=='https:'||!paymentHosts.has(url.hostname)||url.username||url.password||url.port||url.hash)throw invalid();
 return value;
}
function preview(data,payload){
 const completedPurchase=payload.purpose==='PURCHASE'&&data?.purpose==='BALANCE'&&Number.isSafeInteger(payload.sourceDraftId)&&payload.sourceDraftId>0;
 if(!data||data.bookingId!==payload.bookingId||(data.purpose!==payload.purpose&&!completedPurchase)||data.confirmation!=='BANK_API_NOT_CONNECTED'||data.automaticConfirmation!==false)throw invalid();
 const amount=cents(data.amount),due=cents(data.dueAmount),paid=cents(data.paidAmount);
 if(data.status==='NO_PAYMENT_REQUIRED'){
  if(amount!==0||due!==0)throw invalid();
  return {purpose:data.purpose,amount,due,paid,qrPath:null,paymentUrl:null};
 }
 if(data.status!=='AWAITING_BANK_CONFIRMATION'||amount<=0||due<=0||amount>due||data.qrPath!==`/merchant-qr/${payload.bookingId}/qr`)throw invalid();
 return {purpose:data.purpose,amount,due,paid,qrPath:data.qrPath,paymentUrl:paymentLink(data.paymentUrl)};
}
function element(tag,text,className){
 const node=document.createElement(tag);if(text!=null)node.textContent=text;if(className)node.className=className;return node;
}

export function clearMerchantQR(){
 generation++;const previous=active;active=null;
 if(previous?.url)URL.revokeObjectURL(previous.url);
 if(previous?.dialog.open)previous.dialog.close();
 previous?.dialog.remove();
}

export async function showMerchantQR(payload,{isCurrent}={}){
 clearMerchantQR();
 if(!Number.isSafeInteger(payload?.bookingId)||payload.bookingId<1||!purposes.has(payload.purpose)||typeof isCurrent!=='function')throw invalid();
 const context={generation,route:route(),isCurrent,dialog:null};if(!current(context))return;
 let result;
 try{result=await api('/merchant-qr/preview',{method:'POST',body:payload,allowSnapshot:false});}
 catch(error){if(current(context))throw error;return;}
 if(!current(context))return;
 const payment=preview(result,payload),dialog=element('dialog',null,'sheet merchant-qr-dialog');
 dialog.setAttribute('aria-label','Оплата гостем');
 dialog.style.cssText='box-sizing:border-box;width:min(420px,calc(100vw - 24px));max-height:90dvh;overflow:auto;padding:20px;border:1px solid var(--line);border-radius:18px;background:var(--panel);color:var(--text);font:inherit;';
 const close=element('button','Закрыть','btn ghost');close.type='button';close.style.minHeight='44px';close.onclick=()=>discard(context);
 const heading=element('h2',(payment.purpose==='DEPOSIT'?'Предоплата за фотосессию':'Оплата фотографий')+' · запись №'+payload.bookingId);
 const amount=element('p','К оплате сейчас: '+money(payment.amount));amount.style.cssText='font-size:24px;font-weight:700;';amount.setAttribute('data-merchant-amount','');
 dialog.append(close,heading,amount);
 if(payment.paid>0)dialog.append(element('p','Подтверждено ранее: '+money(payment.paid)));
 if(payment.amount===0){
  dialog.append(element('p','Сумма покрыта подтверждёнными поступлениями. Новый платёж не нужен.'));
 }else{
  if(payment.amount<payment.due)dialog.append(element('p','Общий остаток по съёмке: '+money(payment.due)));
  const status=element('p','Загружаем QR…');status.setAttribute('role','status');status.setAttribute('aria-live','polite');dialog.append(status);
  const details=element('div');dialog.append(details);
  const figure=element('div');figure.style.cssText='box-sizing:border-box;width:max-content;max-width:100%;padding:12px;margin:16px auto;background:#fff;border-radius:8px;';
  const image=element('img');image.alt='QR для оплаты через СБП или SberPay';image.width=300;image.height=300;image.style.cssText='display:block;width:300px;max-width:100%;height:auto;object-fit:contain;background:#fff;';
  figure.append(image);
  dialog.append(element('p','Автоматическая проверка банком пока не подключена. После оплаты сообщите сотруднику: поступление нужно сверить перед выдачей фотографий.'));
  dialog.append(element('p','Открытие QR не подтверждает оплату.'));
  context.image=image;context.figure=figure;context.details=details;context.status=status;
 }
 context.dialog=dialog;active={dialog,url:null,context};document.body.append(dialog);
 dialog.addEventListener('close',()=>{if(active?.dialog===dialog)clearMerchantQR();});
 dialog.addEventListener('cancel',event=>{event.preventDefault();if(active?.dialog===dialog)clearMerchantQR();});
 try{dialog.showModal();}catch(error){clearMerchantQR();throw error;}
 if(payment.amount===0)return;
 try{
  const blob=await api(payment.qrPath,{responseType:'blob',allowSnapshot:false});if(!current(context)){discard(context);return;}
  if(!blob||blob.type!=='image/png'||!Number.isFinite(blob.size)||blob.size<=0||blob.size>2*1024*1024)throw new Error('QR временно недоступен. Откройте его заново.');
  const url=URL.createObjectURL(blob);if(!current(context)){URL.revokeObjectURL(url);discard(context);return;}
  active.url=url;
  context.details.append(element('p','В приложении банка введите ровно '+money(payment.amount)+'. QR постоянный, сумма в нём не задана.'),context.figure);
  if(payment.paymentUrl){
   const link=element('a','Оплатить на этом телефоне','btn primary');link.href=payment.paymentUrl;link.target='_blank';link.rel='noopener noreferrer';link.referrerPolicy='no-referrer';
   link.addEventListener('click',event=>{if(!current(context)){event.preventDefault();discard(context);}});
   context.details.append(link,element('p','На другом телефоне отсканируйте QR. На этом телефоне нажмите кнопку оплаты и выберите банк.'));
  }else context.details.append(element('p','Отсканируйте QR другим телефоном в приложении вашего банка.'));
  context.image.onerror=()=>unavailable(context,'QR не загрузился. Закройте окно и откройте QR заново.');
  context.image.src=url;context.status.textContent='Сумма к оплате: '+money(payment.amount)+'.';
 }catch(error){unavailable(context,error.message||'QR временно недоступен. Закройте окно и откройте QR заново.');}
}

window.addEventListener('hashchange',clearMerchantQR);
window.addEventListener('pagehide',clearMerchantQR);
window.addEventListener('pb-operations-reset',clearMerchantQR);
window.addEventListener('pb-operations-stale',clearMerchantQR);
document.addEventListener('pb-chat-account-reset',clearMerchantQR);
window.addEventListener('pb-workflow-mounted',()=>{if(active&&!current(active.context))clearMerchantQR();});
