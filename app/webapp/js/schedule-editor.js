import {esc,addDays,dateLabel} from './domain.js';

const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
let hotels=[],employees=[],minimum='',team=false;
const employeeOptions=selected=>employees.map(e=>`<option value="${e.id}" ${String(selected)===String(e.id)?'selected':''}>${esc(e.name)}</option>`).join('');
const hotelOptions=selected=>hotels.map(h=>`<option value="${h.id}" ${String(selected)===String(h.id)?'selected':''}>${esc(h.name)}</option>`).join('');
function slotMarkup({hotelId=hotels[0]?.id,date=minimum,start='09:00',end='19:00',userId=employees[0]?.id}={}){
 return `<fieldset class="shift-slot panel panel-pad"><legend>${esc(dateLabel(date,{weekday:'short',day:'numeric',month:'short'}))}</legend><div class="form-row">${team?field('Сотрудник',`<select class="input" data-slot="userId" required>${employeeOptions(userId)}</select>`):''}${field('Дата',`<input type="date" class="input" data-slot="date" value="${esc(date)}" min="${minimum}" required>`)}${field('Отель',`<select class="input" data-slot="hotelId" required>${hotelOptions(hotelId)}</select>`)}</div><div class="form-row">${field('Начало · Москва',`<input type="time" class="input" data-slot="start" value="${esc(start)}" required>`)}${field('Окончание · Москва',`<input type="time" class="input" data-slot="end" value="${esc(end)}" required>`)}</div><button class="btn small ghost" type="button" data-action="remove-shift-slot">Удалить смену</button></fieldset>`;
}
export function scheduleEditor(data,day,today,group=false){
 hotels=data.hotels;employees=data.employees;team=group;minimum=today;day=day<today?today:day;
 return `<form id="shiftForm" class="form-grid" data-team="${team}">${team?`<p class="small">Выберите сотрудников для шаблона. Уже добавленные смены других сотрудников сохранятся.</p><div class="row wrap">${employees.map(e=>`<label><input type="checkbox" name="teamUser" value="${e.id}"> ${esc(e.name)}</label>`).join('')}</div>`:field('Сотрудник · один на весь график',`<select name="userId" class="input" required>${employeeOptions()}</select>`)}<div class="form-row">${field('С даты',`<input type="date" class="input" name="from" value="${day}" min="${today}" required>`)}${field('По дату',`<input type="date" class="input" name="to" value="${day}" min="${today}" required>`)}</div><div class="row wrap">${[['1','Пн'],['2','Вт'],['3','Ср'],['4','Чт'],['5','Пт'],['6','Сб'],['0','Вс']].map(([n,label])=>`<label><input type="checkbox" name="weekday" value="${n}" checked> ${label}</label>`).join('')}</div><details><summary>Шаблон для выбранных дней</summary><div class="form-grid section">${field('Отель по шаблону',`<select class="input" name="templateHotel">${hotelOptions()}</select>`)}<div class="form-row">${field('Начало','<input type="time" name="templateStart" class="input" value="09:00" required>')}${field('Окончание','<input type="time" name="templateEnd" class="input" value="19:00" required>')}</div><button class="btn ghost" type="button" data-action="fill-shift-days">Заполнить период по шаблону</button><button class="btn ghost" type="button" data-action="append-shift-days">Добавить ещё один шаблон к периоду</button></div></details><p class="small">До 31 дня. В каждой смене можно изменить отель и время. Для второго отеля в тот же день добавь ещё одну смену.</p><div id="shiftSlots" class="stack">${team?'':slotMarkup({date:day})}</div><button class="btn ghost" type="button" data-action="add-shift-slot">Добавить смену</button><p id="formError" class="form-error" hidden></p><button class="btn primary" type="submit">Сохранить весь график</button></form>`;
}

export function scheduleEditorAction(name,button){
 const form=document.querySelector('#shiftForm'),host=form?.querySelector('#shiftSlots');if(!host)return false;
 if(name==='remove-shift-slot'){button.closest('.shift-slot').remove();return true;}
 const slot={date:form.elements.from.value,hotelId:form.elements.templateHotel.value,start:form.elements.templateStart.value,end:form.elements.templateEnd.value};
 const selected=team?Array.from(form.querySelectorAll('[name=teamUser]:checked'),e=>e.value):[null];
 const maximum=team?620:62;
 if(name==='add-shift-slot'){
  if(host.children.length>=maximum)throw new Error(`В одном сохранении — не более ${maximum} смен.`);
  host.insertAdjacentHTML('beforeend',slotMarkup({...slot,userId:selected[0]||employees[0]?.id}));return true;
 }
 if(!['fill-shift-days','append-shift-days'].includes(name))return false;
 if(!selected.length)throw new Error('Выберите сотрудников для шаблона.');
 const from=form.elements.from.value,to=form.elements.to.value,days=Array.from(form.querySelectorAll('[name=weekday]:checked'),e=>Number(e.value));
 if(!from||!to||from<minimum||from>to||(Date.parse(to)-Date.parse(from))/86400000>30||!days.length)throw new Error('Выбери даты в пределах 31 дня и хотя бы один день недели.');
 const slots=[];for(let date=from;date<=to;date=addDays(date,1))if(days.includes(new Date(date+'T12:00:00Z').getUTCDay()))for(const userId of selected)slots.push({...slot,date,userId});
 if(!slots.length)throw new Error('В выбранном периоде нет выбранных дней недели.');
 const retained=name==='fill-shift-days'?Array.from(host.children).filter(row=>team&&!selected.includes(row.querySelector('[data-slot=userId]').value)):Array.from(host.children);
 if(slots.length+retained.length>maximum)throw new Error(`В одном сохранении — не более ${maximum} смен.`);
 if(name==='fill-shift-days')for(const row of Array.from(host.children))if(!retained.includes(row))row.remove();
 host.insertAdjacentHTML('beforeend',slots.map(slotMarkup).join(''));return true;
}

export function schedulePayload(form){
 const slots=Array.from(form.querySelectorAll('.shift-slot'),row=>Object.fromEntries(Array.from(row.querySelectorAll('[data-slot]'),input=>[input.dataset.slot,input.value])));
 if(!slots.length)throw new Error('Добавь хотя бы одну смену.');
 const sorted=slots.slice().sort((a,b)=>(a.userId||'').localeCompare(b.userId||'')||a.date.localeCompare(b.date)||a.start.localeCompare(b.start));
 if((Math.max(...slots.map(s=>Date.parse(s.date)))-Math.min(...slots.map(s=>Date.parse(s.date))))/86400000>30)throw new Error('Выбери период не более 31 дня.');
 for(let i=0;i<sorted.length;i++){
  const slot=sorted[i];if(slot.start>=slot.end)throw new Error(`Проверь время смены ${slot.date}: окончание должно быть позже начала.`);
  if(i&&sorted[i-1].userId===slot.userId&&sorted[i-1].date===slot.date&&sorted[i-1].end>slot.start)throw new Error(`Смены ${slot.date} пересекаются по времени.`);
 }
 return form.dataset.team==='true'?{shifts:slots}:{userId:form.elements.userId.value,slots};
}
