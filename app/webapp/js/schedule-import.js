import {api as request} from './api.js';
import {esc,ROLE_NAMES,dateLabel} from './domain.js';

const names={MANAGER:'Менеджер',PHOTOGRAPHER:'Фотограф',OFF:'Выходной'};
const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
export function scheduleTextEditor(data){
 const employees=data.employees||[],hotels=data.hotels||[];
 if(!employees.length)return '';
 return `<details class="panel panel-pad schedule-text-panel"><summary><strong>Вставить график одним текстом</strong></summary>
 <form id="scheduleTextForm" class="form-grid section">
 <fieldset class="form-grid" data-import-fields>
 ${field('Сотрудник · один на весь график',`<select class="input" name="employee" required><option value="">Выберите сотрудника</option>${employees.map(e=>`<option value="${e.id}">${esc(e.name)} · №${e.id} · ${esc((e.roles||[]).map(r=>ROLE_NAMES[r]||r).join(' / '))}</option>`).join('')}</select>`)}
 ${field('График · даты с годом и рабочие роли',`<textarea name="scheduleText" class="input" rows="10" maxlength="16000" required placeholder="Период: 22.10.2026–24.10.2026&#10;22.10.2026 — менеджер.&#10;23.10.2026 — фотограф.&#10;24.10.2026 — выходной."></textarea>`)}
 <p class="small">До 31 дня, включая выходные. Вставьте только график, без задания для разработчика. Отель и часы берутся из сохранённого графика, когда их можно определить однозначно. Права сотрудника и факт работы не меняются.</p>
 <details data-import-defaults><summary>Общий отель и часы · если нужно уточнить</summary><div class="form-grid section">
 ${field('Отель для всего периода',`<select class="input" name="hotelId"><option value="">Использовать подтверждённые настройки</option>${hotels.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join('')}</select>`)}
 <div class="form-row">${field('Начало · Москва','<input class="input" type="time" name="start">')}${field('Окончание · Москва','<input class="input" type="time" name="end">')}</div>
 <p class="small">Заполненные поля применятся ко всем рабочим дням периода. Пустые поля не подставляют время по умолчанию.</p></div></details>
 <button type="submit" class="btn ghost">Проверить график</button>
 <div data-import-preview class="stack" aria-live="polite"></div>
 <button type="button" class="btn primary" data-import-apply disabled hidden>Применить весь график</button>
 </fieldset>
 <p data-import-message class="small" role="status" aria-live="polite"></p>
 </form></details>`;
}
function payload(form){
 return {userId:Number(form.elements.employee.value),text:form.elements.scheduleText.value,
  defaults:Object.fromEntries(['hotelId','start','end'].map(k=>[k,form.elements[k].value]).filter(([,v])=>v))};
}
function message(form,text,error=false){
 const target=form.querySelector('[data-import-message]');target.textContent=text;target.className=error?'form-error':'small';
}
function invalidate(form){
 form._version=(form._version||0)+1;form._preview=null;
 const apply=form.querySelector('[data-import-apply]');apply.disabled=true;apply.hidden=true;
 form.querySelector('[data-import-preview]').innerHTML='';
 message(form,'Текст или параметры изменены. Нажмите «Проверить график».');
}
function summary(c){return `Всего ${c.total} дней · Менеджер ${c.MANAGER} · Фотограф ${c.PHOTOGRAPHER} · Выходные ${c.OFF}`;}
function showPreview(form,data){
 const c=data.counts;
 form.querySelector('[data-import-preview]').innerHTML=`<p><strong>${esc(data.employee.name)} · №${data.employee.id}</strong></p><p><strong>${summary(c)}</strong></p>
 ${(data.warnings||[]).map(w=>`<p class="notice">${esc(w)}</p>`).join('')}
 ${data.days.map(d=>`<div class="panel panel-pad"><strong>${esc(dateLabel(d.date))} — ${names[d.role]}</strong><p class="small">${esc(d.hotel||'Нужно выбрать отель')}${d.role==='OFF'?'':` · ${esc(d.start||'?')}–${esc(d.end||'?')}`}${d.factsPreserved?' · Только план; факт сохранится':''}</p></div>`).join('')}`;
 const apply=form.querySelector('[data-import-apply]');
 apply.hidden=!data.canApply;apply.disabled=!data.canApply;
 if(data.needs.length){
  form.querySelector('[data-import-defaults]').open=true;
  const labels={hotelId:'отель',start:'время начала',end:'время окончания'};
  message(form,`Укажите один раз для всего графика: ${data.needs.map(k=>labels[k]).join(', ')}. Затем повторите проверку.`,true);
 }else message(form,'Проверьте все даты. Кнопка ниже заменит только план выбранного сотрудника в указанном периоде.');
}
document.addEventListener('input',event=>{
 const form=event.target.closest?.('#scheduleTextForm');if(form&&!form._busy)invalidate(form);
});
document.addEventListener('change',event=>{
 const form=event.target.closest?.('#scheduleTextForm');if(form&&!form._busy)invalidate(form);
});
document.addEventListener('submit',async event=>{
 const form=event.target;if(form.id!=='scheduleTextForm')return;event.preventDefault();if(form._busy)return;
 form._busy=true;const fields=form.querySelector('[data-import-fields]');fields.disabled=true;
 const version=form._version||0;
 try{
  const body=payload(form);
  const data=await request('/schedule/text/preview',{method:'POST',body,allowSnapshot:false});
  if(!form.isConnected||(form._version||0)!==version)return;
  form._preview=data;form._body=body;form._requestKey=crypto.randomUUID();
  showPreview(form,data);
 }catch(error){form._preview=null;const b=form.querySelector('[data-import-apply]');b.hidden=true;b.disabled=true;message(form,error.message,true);}
 finally{form._busy=false;fields.disabled=false;}
});
function comparable(days){
 return JSON.stringify(days.map(d=>[d.userId,d.date,d.role,d.hotelId,d.start,d.end]));
}
document.addEventListener('click',async event=>{
 const button=event.target.closest?.('[data-import-apply]');if(!button)return;
 const form=button.closest('#scheduleTextForm');if(form._busy||!form._preview?.canApply)return;
 form._busy=true;const fields=form.querySelector('[data-import-fields]');fields.disabled=true;
 try{
  const body={...form._body,revision:form._preview.revision,requestKey:form._requestKey};
  await request('/schedule/text/apply',{method:'POST',body,allowSnapshot:false});
  const saved=await request('/schedule/text/verify',{method:'POST',body:{requestKey:form._requestKey},allowSnapshot:false});
  if(saved.verified!==true||comparable(saved.days)!==comparable(form._preview.days)||summary(saved.counts)!==summary(form._preview.counts)){
   throw new Error('Не удалось подтвердить все даты с сервера. Повторите проверку сохранения, не создавая новый график.');
  }
  message(form,`График сохранён и повторно проверен на сервере. ${summary(saved.counts)}.`);
  button.hidden=true;button.disabled=true;form._preview=null;
  document.dispatchEvent(new CustomEvent('pb-schedule-imported',{detail:{from:saved.from,to:saved.to}}));
 }catch(error){
  message(form,`${error.message} Успешное сохранение пока не подтверждено.`,true);
  if(error.status===409){form._preview=null;button.hidden=true;button.disabled=true;}
 }finally{form._busy=false;fields.disabled=false;}
});
