import {api} from './api.js';
import {esc,dateLabel} from './domain.js';

let dialog=null,version=0;
const close=()=>{dialog?.remove();dialog=null;};
document.addEventListener('pb-chat-account-reset',()=>{version++;close();});
window.addEventListener('hashchange',()=>{version++;close();});
const dismiss=()=>{version++;close();};

export async function openScheduleHours(userId,day,me){
 const ticket=++version;close();
 const data=await api('/schedule?'+new URLSearchParams({from:day,to:day}),{allowSnapshot:false});
 if(ticket!==version)return;
 const person=data.employees.find(p=>p.id===userId);if(!person)throw Error('Сотрудник недоступен для планирования.');
 const items=data.items.filter(s=>s.userId===userId&&s.status!=='CANCELLED'&&!s.planOnly&&!s.monthlyPlan&&typeof s.id==='number');
 const editable=s=>s.status==='PLANNED'&&s.attendance==='PLANNED'&&s.date>=me.today;
 dialog=document.createElement('dialog');dialog.className='month-day-dialog';
 const header=`<header><div><h2>${esc(person.name)}</h2><p>${esc(dateLabel(day))} · Москва</p></div><button type="button" class="icon-button" data-hours-close aria-label="Закрыть часы смены">×</button></header>`;
 const list=()=>items.length?items.map(s=>`<article class="panel panel-pad"><strong>${esc(s.start)}–${esc(s.end)}</strong><p>${esc(s.hotel)}</p>${editable(s)?`<div class="row wrap"><button type="button" class="btn ghost" data-hours-edit="${s.id}">Изменить</button><button type="button" class="btn ghost" data-hours-cancel="${s.id}">Отменить смену</button></div>`:'<p class="small">Фактические и прошедшие смены сохранены.</p>'}</article>`).join(''):'<p>Часы смены ещё не указаны.</p>';
 const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
 function render(shift){
  if(!dialog)return;
  dialog.innerHTML=header+`<div class="stack" data-hours-body>${shift===undefined?list():''}${day>=me.today&&data.hotels.length?shift===undefined?'<button type="button" class="btn primary" data-hours-new>Добавить смену</button>':`<form class="stack" id="monthHoursForm">${field('Отель',`<select class="input" name="hotelId" required><option value="">Выберите отель</option>${data.hotels.map(h=>`<option value="${h.id}" ${h.id===shift?.hotelId?'selected':''}>${esc(h.name)}</option>`).join('')}</select>`)}${field('Дата',`<input class="input" name="date" type="date" value="${esc(shift?.date||day)}" min="${me.today}" required>`)}<div class="form-row">${field('Начало · Москва',`<input class="input" name="start" type="time" value="${esc(shift?.start||'')}" required>`)}${field('Окончание · Москва',`<input class="input" name="end" type="time" value="${esc(shift?.end||'')}" required>`)}</div><p class="small">Часы не меняют отметки о выходе и начисления. Сохранение часов отметит этот день рабочим в месячном графике.</p><button type="submit" class="btn primary">Сохранить часы</button><button type="button" class="btn ghost" data-hours-back>Вернуться к сменам</button></form>`:''}<p class="small" data-hours-status role="status"></p></div>`;
  dialog.querySelector('[data-hours-close]').onclick=dismiss;
  dialog.querySelector('[data-hours-new]')?.addEventListener('click',()=>render(null));
  dialog.querySelector('[data-hours-back]')?.addEventListener('click',()=>render());
  dialog.querySelectorAll('[data-hours-edit]').forEach(b=>b.onclick=()=>render(items.find(s=>s.id===Number(b.dataset.hoursEdit))));
  dialog.querySelectorAll('[data-hours-cancel]').forEach(b=>b.onclick=async()=>{
   const s=items.find(s=>s.id===Number(b.dataset.hoursCancel));
   if(!confirm(`Отменить запланированную смену ${person.name} ${s.date}, ${s.start}–${s.end}? Отметки и история сохранятся.`))return;
   b.disabled=true;try{await api('/schedule/'+s.id,{method:'DELETE',body:{},allowSnapshot:false});if(ticket===version){close();document.dispatchEvent(new CustomEvent('pb-schedule-imported',{detail:{from:day,to:day}}));}}
   catch(error){if(b.isConnected){b.disabled=false;dialog.querySelector('[data-hours-status]').textContent=error.message;}}
  });
  dialog.querySelector('form')?.addEventListener('submit',async event=>{
   event.preventDefault();const form=event.target,button=form.querySelector('[type=submit]'),body=Object.fromEntries(new FormData(form));body.hotelId=Number(body.hotelId);
   if(body.start>=body.end){dialog.querySelector('[data-hours-status]').textContent='Окончание должно быть позже начала.';return;}
   form.querySelectorAll('button,input,select').forEach(el=>el.disabled=true);
   const previous=shift?Object.fromEntries(['hotelId','date','start','end'].map(k=>[k,shift[k]])):null;
   try{await api('/schedule'+(shift?'/'+shift.id:''),{method:shift?'PATCH':'POST',body:shift?{...body,previous}:{...body,userId},allowSnapshot:false});if(ticket===version){close();document.dispatchEvent(new CustomEvent('pb-schedule-imported',{detail:{from:body.date,to:body.date}}));}}
   catch(error){if(form.isConnected){form.querySelectorAll('button,input,select').forEach(el=>el.disabled=false);dialog.querySelector('[data-hours-status]').textContent=error.message+' Введённые часы сохранены в форме. При потере ответа проверьте список смен перед повтором.';}}
  });
 }
 render();dialog.addEventListener('cancel',dismiss);document.body.append(dialog);dialog.showModal();
}
