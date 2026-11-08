import {api} from './api.js';
import {esc,todayKey} from './domain.js';

const labels={R:'Рабочий день',V:'Выходной','.':'Не указано'};
const groups={MANAGER:'Менеджеры',PHOTOGRAPHER:'Фотографы'};
let accountVersion=0,dayDialog=null;
const plans=new Map(),editing=new Set();
const identity=p=>`${p.actorId||0}:${p.month}`;
const draftKey=p=>'pb.scheduleDraft:'+identity(p);
function draft(p){try{const rows=JSON.parse(sessionStorage.getItem(draftKey(p))||'[]');return new Map(rows.filter(x=>x&&/^[a-f0-9]{24}$/.test(x.rowKey)&&/^20\d\d-\d\d-\d\d$/.test(x.date)&&['R','V','.'].includes(x.previous)&&['R','V','.'].includes(x.status)).map(x=>[x.rowKey+':'+x.date,x]));}catch{return new Map();}}
function saveDraft(p,rows){try{if(rows.size)sessionStorage.setItem(draftKey(p),JSON.stringify([...rows.values()]));else sessionStorage.removeItem(draftKey(p));}catch{}}
function closeDay(){dayDialog?.remove();dayDialog=null;}
function resetMonth(){accountVersion++;closeDay();plans.clear();editing.clear();}
document.addEventListener('pb-chat-account-reset',resetMonth);
window.addEventListener('pb-operations-reset',resetMonth);
window.addEventListener('hashchange',closeDay);

export function monthDayRows(plan,day,actualRows){
 if(!plan||day.slice(0,7)!==plan.month)return actualRows;
 const result=actualRows.map(row=>({...row}));
 for(const [index,person] of plan.rows.entries()){
  const status=person.days[Number(day.slice(-2))-1];if(!['R','V'].includes(status))continue;
  const existing=person.userId==null?[]:result.filter(row=>row.userId===person.userId);
  if(existing.length){for(const row of existing)row.monthlyDayStatus=status;continue;}
  result.push({id:`month-${index}-${day}`,userId:person.userId,name:person.name,
   hotel:'',hotelId:null,date:day,role:status==='V'?'OFF':person.group,
   status:status==='V'?'DAY_OFF':'PLAN_ONLY',attendance:status==='V'?'DAY_OFF':'PLAN_ONLY',
   start:'',end:'',monthlyPlan:true,monthlyDayStatus:status,breaks:[]});
 }
 return result;
}

export function scheduleMonthView(plan){
 if(!plan)return '';
 plans.set(identity(plan),plan);const pending=draft(plan),isEditing=editing.has(identity(plan));
 const [year,month]=plan.month.split('-').map(Number),length=new Date(Date.UTC(year,month,0)).getUTCDate();
 const dates=Array.from({length},(_,i)=>`${plan.month}-${String(i+1).padStart(2,'0')}`);
 const title=new Date(Date.UTC(year,month-1,1)).toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'});
 const cell=(row,day,i)=>{const change=pending.get(row.key+':'+day),status=change?.status||row.days[i]||'.',text=labels[status],today=day===todayKey(),label=esc(`${row.name}, ${day}: ${text}${change?' · не сохранено':''}`);return `<td class="month-day month-day-${status==='R'?'work':status==='V'?'off':'unknown'}${today?' month-day-today':''}${change?' month-day-draft':''}">${isEditing&&row.editable?`<button type="button" data-month-cell="${esc(row.key)}" data-date="${day}" aria-label="${label}" title="${label}">${status==='.'?'—':status==='R'?'Р':'В'}</button>`:`<span aria-label="${label}" title="${label}">${status==='.'?'—':status==='R'?'Р':'В'}</span>`}</td>`;};
 const table=plan.rows.length?`<div class="month-plan-scroll" role="region" aria-label="График на ${esc(title)}" tabindex="0"><table class="month-plan-table"><thead><tr><th scope="col" class="month-person">Сотрудник</th>${dates.map((day,i)=>`<th scope="col" class="${day===todayKey()?'month-day-today':''}"><span>${i+1}</span><small>${new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{weekday:'short',timeZone:'UTC'})}</small></th>`).join('')}<th scope="col">Рабочих<br>дней</th></tr></thead>${Object.entries(groups).map(([role,name])=>{const rows=plan.rows.filter(row=>row.group===role);return rows.length?`<tbody><tr class="month-plan-group"><th colspan="${length+2}" scope="colgroup">${name}</th></tr>${rows.map(row=>`<tr><th scope="row" class="month-person">${esc(row.name)}${plan.canEdit&&row.userId==null?'<small>Аккаунт не привязан</small>':''}</th>${dates.map((day,i)=>cell(row,day,i)).join('')}<td class="month-total">${dates.filter((day,i)=>(pending.get(row.key+':'+day)?.status||row.days[i])==='R').length}</td></tr>`).join('')}</tbody>`:'';}).join('')}</table></div>`:`<p class="small">${plan.canEdit?'На этот месяц рабочие и выходные дни ещё не внесены.':'Личный график на этот месяц ещё не внесён.'} Сохранённые смены показаны ниже.</p>`;
 return `<section class="panel panel-pad section month-plan${isEditing?' month-is-editing':''}" id="scheduleMonthPlan" data-month="${esc(plan.month)}" data-actor="${plan.actorId||0}"><div class="month-plan-title"><button type="button" class="icon-button" data-month-step="-1" aria-label="Предыдущий месяц">‹</button><h2>${esc(title)}</h2><button type="button" class="icon-button" data-month-step="1" aria-label="Следующий месяц">›</button></div>${plan.canEdit?`<div class="month-edit-toolbar"><button type="button" class="btn ${isEditing?'primary':'ghost'}" data-month-edit aria-pressed="${isEditing}">${isEditing?'Закончить редактирование':'Изменить график'}</button><button type="button" class="btn ghost" data-month-add>Добавить сотрудника</button></div>${isEditing?'<p class="small">Нажмите на день сотрудника и выберите: рабочий, выходной или не указано.</p>':''}`:''}<div class="month-plan-legend"><span><i class="month-day-work"></i>Рабочий день</span><span><i class="month-day-off"></i>Выходной</span><span><i class="month-day-unknown"></i>Не указано</span></div>${plan.error?`<p role="alert">${esc(plan.error)}</p>`:''}${table}${plan.rows.length?`<p class="small month-plan-note">${esc(plan.source)}. Пустые клетки оставлены без назначения. Часы, отели и фактические отметки смен сохраняются отдельно.</p>`:''}${pending.size?`<div class="month-save-bar"><strong>Изменений: ${pending.size}</strong><button type="button" class="btn primary" data-month-save>Сохранить график</button><button type="button" class="btn ghost" data-month-discard>Отменить изменения</button></div>`:''}<p class="small" role="status" data-month-status></p></section>`;
}

document.addEventListener('click',async event=>{
 const button=event.target.closest?.('[data-month-step]'),panel=button?.closest('#scheduleMonthPlan');if(!panel)return;
 const version=accountVersion,[year,month]=panel.dataset.month.split('-').map(Number),next=new Date(Date.UTC(year,month-1+Number(button.dataset.monthStep),1)).toISOString().slice(0,7);
 panel.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const plan=await api('/schedule/month?'+new URLSearchParams({month:next}),{allowSnapshot:true});if(panel.isConnected&&version===accountVersion)panel.outerHTML=scheduleMonthView(plan);}
 catch(error){if(panel.isConnected&&version===accountVersion)panel.querySelector('[data-month-status]').textContent=error.message;}
 finally{if(panel.isConnected)panel.querySelectorAll('button').forEach(b=>b.disabled=false);}
});

function currentPanel(button){const panel=button.closest('#scheduleMonthPlan'),plan=panel&&plans.get(`${panel.dataset.actor}:${panel.dataset.month}`);return {panel,plan};}
function renderMonth(panel,plan){if(panel?.isConnected)panel.outerHTML=scheduleMonthView(plan);}
function dayEditor(panel,plan,key,day){
 const row=plan.rows.find(r=>r.key===key);if(!row?.editable)return;
 closeDay();const pending=draft(plan),selected=pending.get(key+':'+day)?.status||row.days[Number(day.slice(-2))-1],version=accountVersion;
 dayDialog=document.createElement('dialog');dayDialog.className='month-day-dialog';
 dayDialog.innerHTML=`<header><div><h2>${esc(row.name)}</h2><p>${esc(new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{day:'numeric',month:'long',weekday:'short',timeZone:'UTC'}))}</p></div><button class="icon-button" type="button" data-day-close aria-label="Закрыть изменение дня">×</button></header><form class="stack" id="monthDayForm">${Object.entries(labels).map(([value,label])=>`<label class="month-status-choice month-status-${value==='R'?'work':value==='V'?'off':'unknown'}"><input type="radio" name="status" value="${value}" ${selected===value?'checked':''}><span>${label}</span></label>`).join('')}<p class="small">Фактические отметки и начисления сохраняются. Если уже заданы часы будущей смены, перед выходным отмените её ниже.</p><button class="btn primary" type="submit">Применить</button>${row.userId!=null?'<button class="btn ghost" type="button" data-day-hours>Отель и часы смены</button>':''}</form>`;
 dayDialog.querySelector('[data-day-close]').addEventListener('click',closeDay);
 dayDialog.addEventListener('cancel',closeDay);
 dayDialog.querySelector('[data-day-hours]')?.addEventListener('click',()=>{closeDay();document.dispatchEvent(new CustomEvent('pb-schedule-hours',{detail:{userId:row.userId,date:day}}));});
 dayDialog.querySelector('form').addEventListener('submit',event=>{event.preventDefault();if(version!==accountVersion)return closeDay();const status=new FormData(event.target).get('status'),previous=row.days[Number(day.slice(-2))-1];if(status===previous)pending.delete(key+':'+day);else pending.set(key+':'+day,{rowKey:key,date:day,previous,status});saveDraft(plan,pending);closeDay();renderMonth(panel,plan);});
 document.body.append(dayDialog);dayDialog.showModal();
}
function addEmployee(panel,plan){
 closeDay();const existing=new Set(plan.rows.map(r=>r.userId)),people=(plan.employees||[]).filter(p=>!existing.has(p.id)),version=accountVersion;
 dayDialog=document.createElement('dialog');dayDialog.className='month-day-dialog';dayDialog.innerHTML=`<header><h2>Добавить в график</h2><button type="button" class="icon-button" data-day-close aria-label="Закрыть добавление сотрудника">×</button></header>${people.length?`<form class="stack"><label class="field">Сотрудник<select class="input" name="employee" required>${people.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></label><label class="field">Рабочая роль<select class="input" name="group" required></select></label><button class="btn primary" type="submit">Добавить</button><p class="small" role="status"></p></form>`:'<p>Все доступные сотрудники уже есть в графике.</p>'}`;
 dayDialog.querySelector('[data-day-close]').addEventListener('click',closeDay);dayDialog.addEventListener('cancel',closeDay);
 const form=dayDialog.querySelector('form');if(form){const roles=()=>{const person=people.find(p=>p.id===Number(form.elements.employee.value));form.elements.group.innerHTML=person.roles.filter(r=>r in groups).map(r=>`<option value="${r}">${r==='MANAGER'?'Менеджер':'Фотограф'}</option>`).join('');};roles();form.elements.employee.addEventListener('change',roles);form.addEventListener('submit',async event=>{event.preventDefault();const button=form.querySelector('button');button.disabled=true;try{const result=await api('/schedule/month/employees',{method:'POST',body:{month:plan.month,userId:Number(form.elements.employee.value),group:form.elements.group.value},allowSnapshot:false});if(version!==accountVersion)return;if(panel.isConnected){closeDay();editing.add(identity(result.plan));renderMonth(panel,result.plan);document.dispatchEvent(new CustomEvent('pb-schedule-imported',{detail:{monthOnly:true,from:plan.month+'-01',to:plan.month+'-01'}}));}}catch(error){if(form.isConnected)form.querySelector('[role=status]').textContent=error.message;}finally{button.disabled=false;}});}
 document.body.append(dayDialog);dayDialog.showModal();
}
document.addEventListener('click',async event=>{
 const button=event.target.closest?.('[data-month-edit],[data-month-cell],[data-month-save],[data-month-discard],[data-month-add]');if(!button)return;
 const {panel,plan}=currentPanel(button);if(!plan?.canEdit)return;
 if(button.hasAttribute('data-month-edit')){const key=identity(plan);if(editing.has(key))editing.delete(key);else editing.add(key);renderMonth(panel,plan);return;}
 if(button.hasAttribute('data-month-cell'))return dayEditor(panel,plan,button.dataset.monthCell,button.dataset.date);
 if(button.hasAttribute('data-month-add'))return addEmployee(panel,plan);
 if(button.hasAttribute('data-month-discard')){saveDraft(plan,new Map());renderMonth(panel,plan);return;}
 const changes=[...draft(plan).values()],version=accountVersion;if(!changes.length)return;
 panel.querySelectorAll('button').forEach(b=>b.disabled=true);
 button.disabled=true;const status=panel.querySelector('[data-month-status]');status.textContent='Сохраняем график…';
 try{if(!navigator.onLine)throw Error('Нет сети. Изменения сохранены на этом телефоне. Когда сеть появится, нажмите «Сохранить график».');const result=await api('/schedule/month',{method:'PATCH',body:{month:plan.month,changes},allowSnapshot:false,timeoutMs:12000});if(version!==accountVersion)return;saveDraft(plan,new Map());renderMonth(panel,result.plan);const fresh=document.querySelector('#scheduleMonthPlan [data-month-status]');if(fresh)fresh.textContent='График сохранён. Изменения записаны в аудит.';document.dispatchEvent(new CustomEvent('pb-schedule-imported',{detail:{monthOnly:true,from:plan.month+'-01',to:plan.month+'-01'}}));}
 catch(error){if(panel.isConnected&&version===accountVersion)status.textContent=error.message+' Изменения остаются на этом устройстве; повторно заполнять их не нужно.';}
 finally{if(panel.isConnected)panel.querySelectorAll('button').forEach(b=>b.disabled=false);}
});
