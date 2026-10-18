import {api} from './api.js';
import {esc} from './domain.js';

let identity=null,version=0,loadVersion=0,data=null,answers=[],step=0,editing=false,busy=false,error='',requestId=null;
function changed(){document.dispatchEvent(new CustomEvent('pb-booking-diagnostic-updated'));}
export function resetBookingDiagnostic(uid){
 if(identity===uid)return;
 identity=uid;version++;loadVersion++;data=null;answers=[];step=0;editing=false;busy=false;error='';requestId=null;
}
export async function loadBookingDiagnostic(){
 const user=identity,ticket=++loadVersion,epoch=version;
 try{
  const result=await api('/academy/diagnostic');
  if(user!==identity||epoch!==version||ticket!==loadVersion)return;
  const same=data?.questions?.map(q=>q.id).join('|')===result.questions?.map(q=>q.id).join('|');
  data=result;error='';
  if(!same){answers=Array(result.questions?.length||0).fill(null);step=0;requestId=null;}
  if(!result.assessment)editing=true;
 }catch(cause){if(user!==identity||epoch!==version||ticket!==loadVersion)return;error=cause.message;}
 changed();
}
function assessmentView(result){
 if(!result)return '';
 return `<div class="section"><h3>${esc(result.levelLabel)} · ${result.score}/100</h3><p>${esc(result.basis)}</p><p class="notice">${esc(result.summary)}</p><div class="coach-skills">${result.skills.map(s=>`<article><strong>${esc(s.label)}</strong><p>${s.score}/100 · Вопросов: ${s.questions}</p><progress max="100" value="${s.score}" aria-label="${esc(s.label)}"></progress></article>`).join('')}</div><p class="small">Повторная диагностика сохранит уроки, оценки и историю диалогов.</p></div>`;
}
export function bookingDiagnosticScreen(){
 const head='<section class="academy-panel section" id="bookingDiagnostic"><span class="academy-kicker">СТАРТОВАЯ ДИАГНОСТИКА</span><h2>С чего начать обучение менеджера</h2><p>10 ситуаций об общении и записи гостей. Это предварительная проверка знаний; практику подтвердит диалог.</p>';
 const warning=error?`<p role="alert">${esc(error)}</p>`:'';
 if(!data)return head+warning+`<p role="status">${error?'Диагностика не загружена.':'Загружаем вопросы…'}</p>${error?'<button class="btn ghost" data-booking-diagnostic-action="reload">Повторить загрузку</button>':''}</section>`;
 if(!editing)return head+warning+assessmentView(data.assessment)+'<button class="btn ghost" data-booking-diagnostic-action="retest">Пройти диагностику повторно</button></section>';
 const question=data.questions?.[step];
 if(!question)return head+warning+'<p>Вопросы временно недоступны.</p></section>';
 const last=step===data.questions.length-1;
 return head+(data.assessment?'<details><summary>Предыдущая предварительная оценка</summary>'+assessmentView(data.assessment)+'</details>':'')+`<p class="small">Вопрос ${step+1} из ${data.questions.length}</p><h3>${esc(question.prompt)}</h3><div role="radiogroup" aria-label="Выберите ответ">${question.options.map((option,index)=>`<button type="button" class="menu-item ${answers[step]===index?'primary':''}" role="radio" aria-checked="${answers[step]===index}" ${busy?'disabled':''} data-booking-diagnostic-choice="${index}"><span>${answers[step]===index?'●':'○'}</span><span>${esc(option)}</span></button>`).join('')}</div>${warning}<div class="row wrap section"><button type="button" class="btn ghost" data-booking-diagnostic-action="back" ${busy||step===0?'disabled':''}>Назад</button><button type="button" class="btn primary" data-booking-diagnostic-action="${last?'submit':'next'}" ${busy||answers[step]===null?'disabled':''}>${busy?'Сохраняем…':last?'Получить начальную оценку':'Следующий вопрос'}</button>${data.assessment?`<button type="button" class="btn ghost" data-booking-diagnostic-action="cancel" ${busy?'disabled':''}>К прошлому результату</button>`:''}</div></section>`;
}
function newRequestId(){
 if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();
 const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);
 return Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-booking-diagnostic-action],[data-booking-diagnostic-choice]');
 if(!button||button.disabled||busy)return;
 if(button.hasAttribute('data-booking-diagnostic-choice')){
  const choice=Number(button.dataset.bookingDiagnosticChoice);
  if(editing&&Number.isInteger(choice)&&choice>=0&&choice<3){answers[step]=choice;requestId=null;error='';changed();}
  return;
 }
 const action=button.dataset.bookingDiagnosticAction;
 if(action==='reload')return loadBookingDiagnostic();
 if(action==='retest'){editing=true;answers=Array(data.questions.length).fill(null);step=0;requestId=null;error='';changed();return;}
 if(action==='cancel'){editing=false;error='';changed();return;}
 if(action==='back'){step=Math.max(0,step-1);error='';changed();return;}
 if(action==='next'&&answers[step]!==null){step=Math.min(data.questions.length-1,step+1);error='';changed();return;}
 if(action!=='submit'||answers.some(value=>!Number.isInteger(value)))return;
 const user=identity,epoch=version;
 busy=true;error='';changed();
 try{
  requestId=requestId||newRequestId();
  const result=await api('/academy/diagnostic',{method:'POST',body:{answers:[...answers],requestId}});
  if(user!==identity||epoch!==version)return;
  loadVersion++;data.assessment=result.assessment;editing=false;requestId=null;changed();
  document.dispatchEvent(new CustomEvent('pb-booking-diagnostic-completed',{detail:{assessment:result.assessment}}));
 }catch(cause){if(user===identity&&epoch===version)error=cause.message;}
 finally{if(user===identity&&epoch===version){busy=false;changed();}}
});
