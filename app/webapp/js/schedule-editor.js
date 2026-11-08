import {scheduleTextEditor} from './schedule-import.js';
import {esc,addDays,dateLabel,ROLE_NAMES,WORK_START,OPEN_END} from './domain.js';

const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
let hotels=[],employees=[],minimum='',team=false;
const employeeLabel=e=>`${e.name}${e.roles?.length?' · '+e.roles.map(role=>ROLE_NAMES[role]||role).join(' / '):''} · №${e.id}`;
const employeeOptions=selected=>employees.map(e=>`<option value="${e.id}" ${String(selected)===String(e.id)?'selected':''}>${esc(employeeLabel(e))}</option>`).join('');
const hotelOptions=selected=>hotels.map(h=>`<option value="${h.id}" ${String(selected)===String(h.id)?'selected':''}>${esc(h.name)}</option>`).join('');
const searchable=value=>String(value).toLocaleLowerCase('ru').replaceAll('ё','е').trim();
function rosterPicker(){
 return `<section class="schedule-roster form-grid" aria-label="Выбор сотрудников">${field('Поиск сотрудника по имени, роли или номеру', '<input class="input" type="search" data-roster-search placeholder="Имя, роль или номер" autocomplete="off" aria-describedby="scheduleRosterCount">')}<p id="scheduleRosterCount" class="small" role="status" aria-live="polite">Показаны все: ${employees.length}.${team?' Выбрано: 0.':''}</p>${team?`<div class="row wrap"><button class="btn small ghost" type="button" data-action="select-roster-visible">Выбрать найденных</button><button class="btn small ghost" type="button" data-action="clear-roster-visible">Снять выбор найденных</button></div><div class="schedule-roster-list" role="group" aria-label="Сотрудники для общего графика">${employees.map(e=>`<label class="schedule-roster-person" data-roster-person="${e.id}"><input type="checkbox" name="teamUser" value="${e.id}"><span>${esc(employeeLabel(e))}</span></label>`).join('')}</div>`:field('Сотрудник · один на весь график',`<select name="userId" class="input" required aria-describedby="scheduleRosterCount">${employeeOptions()}</select>`)}</section>`;
}
function filterRoster(form){
 const query=searchable(form.querySelector('[data-roster-search]')?.value||'');
 const found=employees.filter(e=>searchable(employeeLabel(e)).includes(query));
 const foundIds=new Set(found.map(e=>String(e.id)));
 const group=form.dataset.team==='true',select=form.elements.userId;
 let preserved=false;
 if(group){
  for(const row of form.querySelectorAll('[data-roster-person]'))row.hidden=!foundIds.has(row.dataset.rosterPerson);
 }else if(select){
  const selected=String(select.value),current=employees.find(e=>String(e.id)===selected);
  preserved=Boolean(current&&!foundIds.has(selected));
  const visible=preserved?[current,...found]:found;
  select.innerHTML=visible.map(e=>`<option value="${e.id}" ${selected===String(e.id)?'selected':''}>${preserved&&selected===String(e.id)?'Выбран: ':''}${esc(employeeLabel(e))}</option>`).join('');
  select.value=selected;
 }
 const count=form.querySelector('#scheduleRosterCount');
 if(count)count.textContent=`${query?`Найдено: ${found.length} из ${employees.length}.`:`Показаны все: ${employees.length}.`}${group?` Выбрано: ${form.querySelectorAll('[name=teamUser]:checked').length}.`:preserved?' Выбранный сотрудник сохранён.':''}`;
}
// The dialog is recreated when it opens; delegate so search never keeps an old
// roster or loses checked employees when a different search hides their rows.
document.addEventListener('input',event=>{
 const form=event.target.closest?.('#shiftForm');
 if(form&&(event.target.matches('[data-roster-search]')||['teamUser','userId'].includes(event.target.name)))filterRoster(form);
});
document.addEventListener('keydown',event=>{
 if(event.key==='Enter'&&event.target.matches?.('#shiftForm [data-roster-search]'))event.preventDefault();
});
function rosterInfo(data){
 const unavailable=data.unavailableEmployees||[];
 return `<section class="panel panel-pad"><p class="small" role="status">Доступно сотрудников: ${employees.length}. Список получен с сервера.</p><details><summary>Не видите нужного сотрудника?</summary><p class="small">В график входят активные фотографы и менеджеры по записи. Привязка к отелю и наличие других смен не скрывают сотрудника.</p>${unavailable.length?`<ul class="small">${unavailable.map(e=>`<li>${esc(employeeLabel(e))}: ${e.reason==='inactive'?'в архиве — восстановите сотрудника':'назначьте роль «Фотограф» или «Менеджер по записи»'}.</li>`).join('')}</ul>`:'<p class="small">Все зарегистрированные сотрудники с рабочей ролью доступны.</p>'}<button class="btn ghost" type="button" data-action="people">Сотрудники и роли</button></details></section>`;
}
function slotMarkup({hotelId=hotels[0]?.id,date=minimum,userId=employees[0]?.id}={}){
 return `<fieldset class="shift-slot panel panel-pad"><legend>${esc(dateLabel(date,{weekday:'short',day:'numeric',month:'short'}))}</legend><div class="form-row">${team?field('Сотрудник',`<select class="input" data-slot="userId" required>${employeeOptions(userId)}</select>`):''}${field('Дата',`<input type="date" class="input" data-slot="date" value="${esc(date)}" min="${minimum}" required>`)}${field('Отель',`<select class="input" data-slot="hotelId" required>${hotelOptions(hotelId)}</select>`)}</div><input type="hidden" data-slot="start" value="${WORK_START}"><input type="hidden" data-slot="end" value="${OPEN_END}"><p class="small muted">С 09:00 до последней продажи.</p><button class="btn small ghost" type="button" data-action="remove-shift-slot">Удалить смену</button></fieldset>`;
}
export function scheduleEditor(data,day,today,group=false){
 hotels=data.hotels||[];employees=data.employees||[];team=group;minimum=today;day=day<today?today:day;
 const roster=rosterInfo(data);
 if(!employees.length||!hotels.length)return `${roster}<p>${!hotels.length?'Добавьте активный отель. ':''}${!employees.length?'Восстановите сотрудника из архива или назначьте ему рабочую роль.':''}</p><button class="btn primary" type="button" data-action="people">Открыть сотрудников</button>`;
 return `${roster}${team?'':scheduleTextEditor(data)}<form id="shiftForm" class="form-grid" data-team="${team}">${team?'<p class="small">Выберите сотрудников для шаблона. Уже добавленные смены других сотрудников сохранятся. Поиск не снимает выбранных сотрудников.</p>':''}${rosterPicker()}<div class="form-row">${field('С даты',`<input type="date" class="input" name="from" value="${day}" min="${today}" required>`)}${field('По дату',`<input type="date" class="input" name="to" value="${day}" min="${today}" required>`)}</div><div class="row wrap">${[['1','Пн'],['2','Вт'],['3','Ср'],['4','Чт'],['5','Пт'],['6','Сб'],['0','Вс']].map(([n,label])=>`<label><input type="checkbox" name="weekday" value="${n}" checked> ${label}</label>`).join('')}</div><details open><summary>Отель для выбранных дней</summary><div class="form-grid section">${field('Отель по шаблону',`<select class="input" name="templateHotel">${hotelOptions()}</select>`)}<input type="hidden" name="templateStart" value="${WORK_START}"><input type="hidden" name="templateEnd" value="${OPEN_END}"><button class="btn ghost" type="button" data-action="fill-shift-days">Заполнить период этим отелем</button><button class="btn ghost" type="button" data-action="append-shift-days">Поставить этот отель на выбранные дни</button><p class="small">«Поставить» добавляет дни к списку; если день уже есть, у него меняется отель.</p></div></details><p class="small">До 31 дня. Все работают с 9:00 до последней продажи — часы указывать не нужно, смену сотрудник закрывает в приложении. В один день — один отель; его можно изменить в каждой смене.</p><div id="shiftSlots" class="stack">${team?'':slotMarkup({date:day})}</div><button class="btn ghost" type="button" data-action="add-shift-slot">Добавить смену</button><p id="formError" class="form-error" hidden></p><button class="btn primary" type="submit">Сохранить весь график</button></form>`;
}

export function scheduleEditorAction(name,button){
 const form=document.querySelector('#shiftForm'),host=form?.querySelector('#shiftSlots');if(!host)return false;
 const handled=scheduleSlotsAction(name,button,form,host);
 // A change to the list answers the last save error; do not leave it on screen.
 if(handled){const error=form.querySelector('#formError');if(error)error.hidden=true;}
 return handled;
}
function scheduleSlotsAction(name,button,form,host){
 if(['select-roster-visible','clear-roster-visible'].includes(name)){
  for(const row of form.querySelectorAll('[data-roster-person]:not([hidden])'))row.querySelector('input').checked=name==='select-roster-visible';
  filterRoster(form);return true;
 }
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
 // One hotel per day: "fill" replaces the selected people's rows, "append"
 // adds days and replaces a day that is already listed for the same person.
 const owner=row=>team?row.querySelector('[data-slot=userId]').value:'';
 const fresh=new Set(slots.map(s=>`${s.userId??''}|${s.date}`));
 const retained=Array.from(host.children).filter(row=>name==='fill-shift-days'
  ?team&&!selected.includes(owner(row))
  :!fresh.has(`${owner(row)}|${row.querySelector('[data-slot=date]').value}`));
 if(slots.length+retained.length>maximum)throw new Error(`В одном сохранении — не более ${maximum} смен.`);
 for(const row of Array.from(host.children))if(!retained.includes(row))row.remove();
 host.insertAdjacentHTML('beforeend',slots.map(slotMarkup).join(''));return true;
}

export function schedulePayload(form){
 const slots=Array.from(form.querySelectorAll('.shift-slot'),row=>Object.fromEntries(Array.from(row.querySelectorAll('[data-slot]'),input=>[input.dataset.slot,input.value])));
 if(!slots.length)throw new Error('Добавь хотя бы одну смену.');
 const sorted=slots.slice().sort((a,b)=>(a.userId||'').localeCompare(b.userId||'')||a.date.localeCompare(b.date)||a.start.localeCompare(b.start));
 if((Math.max(...slots.map(s=>Date.parse(s.date)))-Math.min(...slots.map(s=>Date.parse(s.date))))/86400000>30)throw new Error('Выбери период не более 31 дня.');
 for(let i=0;i<sorted.length;i++){
  const slot=sorted[i],day=dateLabel(slot.date,{weekday:'short',day:'numeric',month:'short'});
  if(slot.start>=slot.end)throw new Error(`Проверь смену ${day}: окончание должно быть позже начала.`);
  if(i&&sorted[i-1].userId===slot.userId&&sorted[i-1].date===slot.date&&sorted[i-1].end>slot.start){
   const person=employees.find(e=>String(e.id)===String(slot.userId))?.name;
   throw new Error(`${person?`У ${person} н`:'Н'}а ${day} уже есть смена: в один день — один отель. Удалите лишнюю.`);
  }
 }
 return form.dataset.team==='true'?{shifts:slots}:{userId:form.elements.userId.value,slots};
}
