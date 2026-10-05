import {esc,addDays,dateLabel} from './domain.js';

const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
let hotels=[],minimum='';
const hotelOptions=selected=>hotels.map(h=>`<option value="${h.id}" ${String(selected)===String(h.id)?'selected':''}>${esc(h.name)}</option>`).join('');
function slotMarkup({hotelId=hotels[0]?.id,date=minimum,start='09:00',end='19:00'}={}){
 return `<fieldset class="shift-slot panel panel-pad"><legend>${esc(dateLabel(date,{weekday:'short',day:'numeric',month:'short'}))}</legend><div class="form-row">${field('Дата',`<input type="date" class="input" data-slot="date" value="${esc(date)}" min="${minimum}" required>`)}${field('Отель',`<select class="input" data-slot="hotelId" required>${hotelOptions(hotelId)}</select>`)}</div><div class="form-row">${field('Начало · Москва',`<input type="time" class="input" data-slot="start" value="${esc(start)}" required>`)}${field('Окончание · Москва',`<input type="time" class="input" data-slot="end" value="${esc(end)}" required>`)}</div><button class="btn small ghost" type="button" data-action="remove-shift-slot">Удалить смену</button></fieldset>`;
}
export function scheduleEditor(data,day,today){
 hotels=data.hotels;minimum=today;day=day<today?today:day;
 return `<form id="shiftForm" class="form-grid">${field('Сотрудник · один на весь график',`<select name="userId" class="input" required>${data.employees.map(e=>`<option value="${e.id}">${esc(e.name)}</option>`).join('')}</select>`)}<div class="form-row">${field('С даты',`<input type="date" class="input" name="from" value="${day}" min="${today}" required>`)}${field('По дату',`<input type="date" class="input" name="to" value="${day}" min="${today}" required>`)}</div><div class="row wrap">${[['1','Пн'],['2','Вт'],['3','Ср'],['4','Чт'],['5','Пт'],['6','Сб'],['0','Вс']].map(([n,label])=>`<label><input type="checkbox" name="weekday" value="${n}" checked> ${label}</label>`).join('')}</div><details><summary>Шаблон для выбранных дней</summary><div class="form-grid section">${field('Отель по шаблону',`<select class="input" name="templateHotel">${hotelOptions()}</select>`)}<div class="form-row">${field('Начало','<input type="time" name="templateStart" class="input" value="09:00" required>')}${field('Окончание','<input type="time" name="templateEnd" class="input" value="19:00" required>')}</div><button class="btn ghost" type="button" data-action="fill-shift-days">Заполнить период по шаблону</button><button class="btn ghost" type="button" data-action="append-shift-days">Добавить ещё один шаблон к периоду</button></div></details><p class="small">До 31 дня. В каждой смене можно изменить отель и время. Для второго отеля в тот же день добавь ещё одну смену.</p><div id="shiftSlots" class="stack">${slotMarkup({date:day})}</div><button class="btn ghost" type="button" data-action="add-shift-slot">Добавить смену</button><p id="formError" class="form-error" hidden></p><button class="btn primary" type="submit">Сохранить весь график</button></form>`;
}

export function scheduleEditorAction(name,button){
 const form=document.querySelector('#shiftForm'),host=form?.querySelector('#shiftSlots');if(!host)return false;
 if(name==='remove-shift-slot'){button.closest('.shift-slot').remove();return true;}
 const slot={date:form.elements.from.value,hotelId:form.elements.templateHotel.value,start:form.elements.templateStart.value,end:form.elements.templateEnd.value};
 if(name==='add-shift-slot'){
  if(host.children.length>=62)throw new Error('В одном сохранении — не более 62 смен.');
  host.insertAdjacentHTML('beforeend',slotMarkup(slot));return true;
 }
 if(!['fill-shift-days','append-shift-days'].includes(name))return false;
 const from=form.elements.from.value,to=form.elements.to.value,days=Array.from(form.querySelectorAll('[name=weekday]:checked'),e=>Number(e.value));
 if(!from||!to||from<minimum||from>to||(Date.parse(to)-Date.parse(from))/86400000>30||!days.length)throw new Error('Выбери даты в пределах 31 дня и хотя бы один день недели.');
 const slots=[];for(let date=from;date<=to;date=addDays(date,1))if(days.includes(new Date(date+'T12:00:00Z').getUTCDay()))slots.push({...slot,date});
 if(!slots.length)throw new Error('В выбранном периоде нет выбранных дней недели.');
 if(slots.length+(name==='append-shift-days'?host.children.length:0)>62)throw new Error('В одном сохранении — не более 62 смен.');
 if(name==='fill-shift-days')host.innerHTML='';
 host.insertAdjacentHTML('beforeend',slots.map(slotMarkup).join(''));return true;
}

export function schedulePayload(form){
 const slots=Array.from(form.querySelectorAll('.shift-slot'),row=>Object.fromEntries(Array.from(row.querySelectorAll('[data-slot]'),input=>[input.dataset.slot,input.value])));
 if(!slots.length)throw new Error('Добавь хотя бы одну смену.');
 const sorted=slots.slice().sort((a,b)=>a.date.localeCompare(b.date)||a.start.localeCompare(b.start));
 if((Date.parse(sorted.at(-1).date)-Date.parse(sorted[0].date))/86400000>30)throw new Error('Выбери период не более 31 дня.');
 for(let i=0;i<sorted.length;i++){
  const slot=sorted[i];if(slot.start>=slot.end)throw new Error(`Проверь время смены ${slot.date}: окончание должно быть позже начала.`);
  if(i&&sorted[i-1].date===slot.date&&sorted[i-1].end>slot.start)throw new Error(`Смены ${slot.date} пересекаются по времени.`);
 }
 return {userId:form.elements.userId.value,slots};
}
