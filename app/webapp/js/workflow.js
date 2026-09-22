import {api} from './api.js';
import {esc,todayKey} from './domain.js';
import {enqueueBatch,outboxRows,retryOperation,flushOutbox} from './outbox.js';
let data=null,queue=[],selected=null;
const labels={local:'Сохранено локально',syncing:'Синхронизируется',synced:'Синхронизировано',error:'Ошибка'};
const kinds={booking:'Бронь',booking_receipt:'Чек брони',sale_start:'Черновик продажи',sale_receipt:'Чек продажи',sale_counts:'Количество кадров',sale_selected:'Выбранный кадр',sale_complete:'Оформление продажи',shoot_photo:'Кадр полной съёмки',shoot_complete:'Завершение загрузки',checklist:'Контрольный пункт',shift_report:'Замечание',attendance:'Отметка смены'};
export async function loadWorkflow(){data=await api('/workflow');queue=await outboxRows();if(selected&&!data.bookings.some(b=>b.id===selected))selected=null;}
const field=(label,input)=>`<label class="field">${label}${input}</label>`;
const input=(name,attrs='')=>`<input class="input" name="${name}" ${attrs}>`;
const file=(name,multiple=false,required=true)=>input(name,`type="file" accept="image/jpeg,image/png,image/webp" ${multiple?'multiple':''} ${required?'required':''}`);
const options=rows=>rows.map(b=>`<option value="${b.id}">${esc(b.name||`№${b.id} · ${b.date} ${b.time} · комната ${b.room}`)}</option>`).join('');
function queueView(){return queue.slice(-100).map(r=>`<article class="panel"><strong>${labels[r.status]}</strong> · ${esc(kinds[r.kind]||r.kind)}${r.result?.bookingId?` · запись №${r.result.bookingId}`:''}${r.result?.saleId?` · продажа №${r.result.saleId}`:''}<p>${esc(r.error||'')}</p>${r.status==='error'||(r.status==='local'&&r.error)?`<button class="btn ghost" data-workflow="retry" data-key="${r.key}">Повторить</button>`:''}</article>`).join('')||'<p>Операций в очереди нет.</p>';}
function connectionNote(){return (data?._offline?`${navigator.onLine?'Сеть доступна. Форма использует сохранённые справочники':'Нет сети. Справочники сохранены'} ${new Date(data._cachedAt).toLocaleString('ru-RU')}. `:'')+'Брони, продажи, чеки и фото сохраняются на этом устройстве. Суммы и права проверяются сервером при синхронизации. До статуса «Синхронизировано» операция ещё не учтена.';}
export function workflowView(){
 const ready=data.bookings.filter(b=>b.status==='READY_FOR_SALE'),b=data.bookings.find(b=>b.id===selected),d=b?.draft;
 const upload=data.bookings.filter(b=>b.status==='READY_FOR_SALE'&&!b.fullUploaded&&b.canUpload);
 return `<h1>Рабочие операции</h1><div id="workflowNotice" class="notice">${esc(connectionNote())}</div>
 ${data.canBook?`<details class="panel section"><summary>Новая бронь</summary><form id="workflowBooking" class="form-grid">${field('Имя гостя',input('client_name','required minlength="2" maxlength="200"'))}${field('Телефон (необязательно)',input('client_phone','type="tel" maxlength="80"'))}${field('Отель',`<select class="input" name="hotel_id" required>${options(data.hotels)}</select>`)}${field('Комната',input('room','required maxlength="100"'))}${field('Гостей',input('guest_count','type="number" min="1" max="100" value="1" required'))}${field('Дата',input('shoot_date',`type="date" value="${todayKey()}" required`))}${field('Время',input('shoot_time','type="time" required'))}${field('Пакет',`<select class="input" name="package_id" required>${options(data.packages)}</select>`)}${field('Заявленная предоплата, ₽',input('deposit','type="number" min="0" max="10000000" step="0.01" value="0" required'))}${field('Чек предоплаты (если внесена)',file('receipt',false,false))}<button class="btn primary">Сохранить бронь</button></form></details>`:''}
 <section class="panel section"><h2>Продажа</h2><p>Выбранные кадры обязательны; оплата подтверждается владельцем.</p><select class="input" id="workflowSalePick"><option value="">Выберите готовую съёмку</option>${ready.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>№${x.id} · ${esc(x.date)} · комната ${esc(x.room)}</option>`).join('')}</select>${b?(d&&!d.mine?'<p>Продажу оформляет другой сотрудник.</p>':`<form id="workflowSale" class="form-grid section"><p>Цена кадра в отеле: ${esc(b.price)} ₽. Итог рассчитает сервер.</p>${!d||d.status==='AWAITING_RECEIPT'?field('Чек продажи',file('receipt')):''}${field('Всего кадров в съёмке',input('total',`type="number" min="1" max="10000" required value="${d?.total||''}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}${field('Продано кадров',input('sold',`type="number" min="1" max="10000" required value="${d?.sold||''}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}${field('Скидка на всю съёмку, % (назначает фотограф)',input('discount',`type="number" min="0" max="50" step="0.01" value="${d?.discount||0}" ${d?.status==='AWAITING_SELECTED'?'readonly':''}`))}<p>Скидка до 50% применяется только при покупке всей съёмки. Слайд-шоу демонстрируется бесплатно.</p><p>Уже сохранено выбранных кадров: ${d?.selected||0}. Прикрепите оставшиеся.</p>${field('Выбранные фотографии',file('selected',true,false))}<button class="btn primary">Сохранить продажу и фотографии</button></form>`):''}</section>
 <details class="panel section"><summary>Загрузить полную съёмку</summary>${upload.length?`<form id="workflowShoot" class="form-grid">${field('Съёмка',`<select class="input" name="booking">${options(upload)}</select>`)}${field('Фотографии',file('photos',true,false))}<label><input type="checkbox" name="finish"> Все кадры добавлены — завершить загрузку</label><p>Можно добавлять частями. Ставка фотографа фиксируется сервером после завершения полной загрузки.</p><button class="btn primary">Сохранить фотографии</button></form>`:'<p>Нет доступных съёмок для полной загрузки.</p>'}</details>
 <section class="section"><h2>Синхронизация</h2><button class="btn ghost" data-workflow="sync">Синхронизировать сейчас</button><div id="workflowQueue">${queueView()}</div></section>`;
}
function imageFiles(form,name,maximum=20*1024*1024){const list=[...form.elements[name].files];for(const f of list)if(!['image/jpeg','image/png','image/webp'].includes(f.type)||!f.size||f.size>maximum)throw new Error(`Изображения JPEG, PNG или WebP, не больше ${maximum/1024/1024} МБ.`);return list;}
export async function workflowSubmit(form){
 const f=new FormData(form),date=todayKey(),items=[],add=(kind,data,blob=null)=>{const row={key:crypto.randomUUID(),kind,date,data,blob};items.push(row);return {key:row.key};};
 if(form.id==='workflowBooking'){
  const receipt=imageFiles(form,'receipt',8*1024*1024)[0],deposit=f.get('deposit');if(Number(deposit)>0&&!receipt)throw new Error('Прикрепите чек заявленной предоплаты.');
  const booking=add('booking',{client_name:f.get('client_name'),client_phone:f.get('client_phone')||null,hotel_id:Number(f.get('hotel_id')),package_id:Number(f.get('package_id')),room:f.get('room'),guest_count:Number(f.get('guest_count')),deposit,shoot_date:f.get('shoot_date'),shoot_time:f.get('shoot_time'),photographer_id:null});
  if(receipt)add('booking_receipt',{booking,purpose:'DEPOSIT'},receipt);
 }else if(form.id==='workflowSale'){
  const b=data.bookings.find(b=>b.id===selected);if(!b)throw new Error('Выберите съёмку.');const d=b.draft,total=Number(f.get('total')),sold=Number(f.get('sold'));
  const photos=imageFiles(form,'selected');if(!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold>total)throw new Error('Проверьте количество кадров.');
  if(photos.length+(d?.selected||0)!==sold)throw new Error(`Нужно прикрепить ${sold-(d?.selected||0)} выбранных кадров.`);
  const draft=add('sale_start',{booking:{id:b.id},price:b.price});
  if(!d||d.status==='AWAITING_RECEIPT'){const receipt=imageFiles(form,'receipt',8*1024*1024)[0];if(!receipt)throw new Error('Прикрепите чек продажи.');add('sale_receipt',{draft},receipt);}
  if(!d||d.status!=='AWAITING_SELECTED')add('sale_counts',{draft,total,sold,price:b.price,discount:f.get('discount')||'0'});
  for(const photo of photos)add('sale_selected',{draft},photo);
  add('sale_complete',{draft,price:b.price});
 }else{
  const b=data.bookings.find(b=>b.id===Number(f.get('booking')));if(!b)throw new Error('Выберите съёмку.');
  for(const photo of imageFiles(form,'photos'))add('shoot_photo',{shooting:{id:b.shootingId}},photo);
  if(f.get('finish'))add('shoot_complete',{shooting:{id:b.shootingId}});
 }
 await enqueueBatch(items);form.reset();queue=await outboxRows();document.querySelector('#workflowQueue').innerHTML=queueView();
}
export async function workflowClick(button){if(button.dataset.workflow==='retry')await retryOperation(button.dataset.key);else await flushOutbox();}
export function workflowSelect(id){selected=Number(id)||null;document.querySelector('#app').innerHTML=workflowView();}
function refreshConnection(){const el=document.querySelector('#workflowNotice');if(el)el.textContent=connectionNote();}
window.addEventListener('online',refreshConnection);window.addEventListener('offline',refreshConnection);
window.addEventListener('pb-outbox',async()=>{refreshConnection();const el=document.querySelector('#workflowQueue');if(el){queue=await outboxRows();el.innerHTML=queueView();}});
