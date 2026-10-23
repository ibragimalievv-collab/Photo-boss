import {api} from './api.js';
import {esc} from './domain.js';
import {enqueueOperation,flushOutbox,outboxRows} from './outbox.js';
let state=null,pendingKey=null,acting=false,generation=0;
const time=value=>value?new Date(value).toLocaleString('ru-RU',{timeZone:state.timezone}):'На обеде';
const local=value=>{if(!value)return '';const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
export async function loadLunch(){const version=generation;try{const latest=await api('/work-breaks');if(version===generation)state=latest;}catch(error){if(version!==generation)return;if(navigator.onLine!==false)throw error;state=null;}}
export function lunchView(){if(!state)return '';const open=state.items.find(r=>r.userId===state.actorId&&!r.endedAt);return `<section id="lunchPanel" class="panel section"><h2>Обед</h2><p>Время отметок: ${esc(state.timezone)}. Текущая отметка сохраняется серверным временем; исправления — только у владельца и администратора своих отелей.</p>${open?`<p><strong>Вы на обеде с ${esc(time(open.startedAt))}</strong></p><button type="button" class="btn primary" data-lunch="end" data-hotel="${open.hotelId}">Вернулся с обеда</button>`:`<label class="field">Отель смены<select id="lunchHotel" class="input">${state.hotels.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join('')}</select></label><button type="button" class="btn primary" data-lunch="start" ${!state.canStart||!state.hotels.length?'disabled':''}>Ушёл на обед</button>${!state.canStart?'<p>Сначала сохраните начало смены с геолокацией и фото. После завершения смены обед недоступен.</p>':state.startPendingReview?'<p>Начало смены записано и ожидает проверки руководителем. Отметки обеда сохраняются отдельно.</p>':''}`}<p id="lunchMessage" role="status"></p>${state.items.map(r=>`<article class="section"><strong>${esc(r.name)} · ${esc(r.hotel)}</strong><p>${esc(time(r.startedAt))} → ${esc(time(r.endedAt))} · ${r.minutes} мин.</p>${state.canEdit?`<details><summary>Исправить отметку</summary>${editForm(r)}</details>`:''}</article>`).join('')||'<p>Отметок обеда за день пока нет.</p>'}${state.canEdit?`<details><summary>Добавить пропущенную отметку</summary>${editForm(null)}</details>`:''}</section>`;}
function editForm(row){return `<form class="lunch-edit form-grid" data-id="${row?.id||''}" data-revision="${esc(row?.revision||'')}">${!row?`<label class="field">Сотрудник<select class="input" name="userId" required>${state.employees.map(u=>`<option value="${u.id}">${esc(u.name)}</option>`).join('')}</select></label><label class="field">Отель<select class="input" name="hotelId">${state.hotels.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join('')}</select></label><label class="field">Дата смены<input class="input" name="date" type="date" value="${state.date}" required></label>`:''}<p>Поля времени ниже — в часовом поясе вашего устройства.</p><label class="field">Ушёл<input class="input" name="startedAt" type="datetime-local" value="${local(row?.startedAt)}" required></label><label class="field">Вернулся (пусто — ещё на обеде)<input class="input" name="endedAt" type="datetime-local" value="${local(row?.endedAt)}"></label><label class="field">Причина исправления<input class="input" name="reason" minlength="3" maxlength="500" required></label><button class="btn primary">Сохранить отметку</button><p role="status"></p></form>`;}
async function refresh(){await loadLunch();const panel=document.querySelector('#lunchPanel');if(panel)panel.outerHTML=lunchView();}
// A saved shift changes lunch eligibility even while this panel is already open.
window.addEventListener('pb-attendance-saved',()=>{if(document.querySelector('#lunchPanel'))refresh().catch(error=>{const message=document.querySelector('#lunchMessage');if(message)message.textContent=error.message;});});
export async function waitForLunch(key,timeout=20000){
 const deadline=Date.now()+timeout;
 for(;;){
  const row=(await outboxRows()).find(r=>r.key===key);
  if(row?.status==='synced')return row;
  if(row?.status==='error')throw new Error(row.error||'Отметка отклонена сервером.');
  if(Date.now()>=deadline)throw new Error('Отметка сохранена в очереди. Результат обновится после отправки.');
  await new Promise(resolve=>setTimeout(resolve,150));
 }
}
window.addEventListener('pb-outbox',async()=>{
 if(!pendingKey)return;
 const key=pendingKey,version=generation;
 const row=(await outboxRows()).find(r=>r.key===key);
 if(version!==generation||key!==pendingKey)return;
 if(row?.status==='synced'){pendingKey=null;await refresh();}
});
function clearLunch(){generation++;state=null;pendingKey=null;acting=false;document.querySelector('#lunchPanel')?.remove();}
window.addEventListener('pb-operations-reset',clearLunch);
document.addEventListener('pb-chat-account-reset',clearLunch);
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-lunch]');if(!button||acting)return;
 const version=generation;
 acting=true;button.disabled=true;
 try{
  if(!navigator.onLine)throw new Error('Для точной отметки обеда нужна сеть. Пропущенное время может добавить руководитель.');
  if(!pendingKey){
   const saved=await enqueueOperation('break_'+button.dataset.lunch,state.date,{hotelId:Number(button.dataset.hotel||document.querySelector('#lunchHotel')?.value)});
   if(version!==generation)return;
   pendingKey=saved.key;
  }
  const key=pendingKey;void flushOutbox();
  await waitForLunch(key);if(version!==generation)return;pendingKey=null;await refresh();
 }catch(error){
  if(version!==generation)return;
  const row=pendingKey?(await outboxRows()).find(r=>r.key===pendingKey):null;
  if(row?.status==='error')pendingKey=null;
  const message=document.querySelector('#lunchMessage');if(message)message.textContent=error.message;
 }finally{if(version===generation){acting=false;button.disabled=false;}}
});
document.addEventListener('submit',async event=>{const form=event.target;if(!form.matches('.lunch-edit'))return;event.preventDefault();const f=new FormData(form),button=form.querySelector('button');button.disabled=true;try{const body={startedAt:new Date(f.get('startedAt')).toISOString(),endedAt:f.get('endedAt')?new Date(f.get('endedAt')).toISOString():null,reason:f.get('reason')};if(form.dataset.id)body.revision=form.dataset.revision;else Object.assign(body,{userId:Number(f.get('userId')),hotelId:Number(f.get('hotelId')),date:f.get('date')});await api('/work-breaks'+(form.dataset.id?'/'+form.dataset.id:''),{method:form.dataset.id?'PUT':'POST',body});await refresh();}catch(error){form.querySelector('[role=status]').textContent=error.message;}finally{button.disabled=false;}});
