import {api} from './api.js';
import {esc} from './domain.js';
import {captureForms,clearForm} from './form-state.js';
import {realtimeControls} from './realtime-training.js';

let identity=null,track='photographer',snapshot=null,answer=null,request=null,error='',version=0;
const requests=new Map();
const voiceSessions=new Map();
let voiceSession=null,voiceBusy=false,voiceError='';
const eventName='pb-academy-mentor-updated';
function changed(){captureForms();document.dispatchEvent(new CustomEvent(eventName));}
function requestKey(){return String(identity)+':'+track;}
function isPending(){return request?.pending===true;}
function newId(){return globalThis.crypto?.randomUUID?.().replaceAll('-','')||('mentor_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2));}
export function resetAcademyMentor(uid){if(uid===identity)return;identity=uid;version++;track='photographer';snapshot=null;answer=null;request=null;error='';requests.clear();voiceSessions.clear();voiceSession=null;voiceBusy=false;voiceError='';}
export function academyMentorSessionId(){return voiceSession?.id||null;}
export async function loadAcademyMentor(selected='photographer'){
 const ticket=++version,user=identity;if(track!==selected){snapshot=null;answer=null;error='';voiceError='';voiceBusy=false;}track=selected;error='';request=requests.get(requestKey())||null;voiceSession=voiceSessions.get(requestKey())||null;
 try{const result=await api('/academy/mentor?track='+encodeURIComponent(selected));if(version!==ticket||identity!==user)return;snapshot={...result,sources:Array.isArray(result.sources)?result.sources:[],history:Array.isArray(result.history)?result.history:[],skills:Array.isArray(result.skills)?result.skills:[]};answer=snapshot.history.find(x=>x.track===selected)||null;changed();}
 catch(failure){if(version!==ticket||identity!==user)return;if(failure.status===401)throw failure;snapshot=null;answer=null;error=failure.message;changed();}
}
function voicePanel(){const offline=snapshot?._offline||globalThis.navigator?.onLine===false,blocked=track==='booking'&&!snapshot?.diagnosticAssessment&&!snapshot?.hasDialogueHistory;return `<section class="notice section"><h3>Разговор с наставником прямо в Академии</h3><p>Можно говорить, перебивать и задавать следующие вопросы. Реплики видны текстом; после остановки они сохраняются в истории обучения.</p>${blocked?'<p>Перед первым разговором менеджера пройдите входную диагностику. Общие текстовые вопросы наставнику уже доступны.</p>':''}<p>Нужны интернет и разрешение на микрофон. Микрофон включится только по кнопке «Начать живой разговор».</p>${voiceSession?.status==='ACTIVE'?realtimeControls(voiceSession,'coach'):`<button class="btn primary" type="button" data-mentor-voice ${voiceBusy||offline||!snapshot?.configured||blocked?'disabled':''}>${voiceBusy?'Открываем разговор…':'Открыть голосового наставника'}</button>`}<p role="alert">${esc(voiceError)}</p>${voiceSession?.transcript?.length?`<details class="section" ${voiceSession.status==='COMPLETED'?'open':''}><summary>Сохранённый текст разговора</summary>${voiceSession.transcript.map(item=>`<p><strong>${item.role==='employee'?'Вы':item.role==='coach'?'Наставник':'Учебный гость'}:</strong> ${esc(item.text)}</p>`).join('')}</details>`:''}${voiceSession?.status==='COMPLETED'?`<p>${esc(voiceSession.evaluation?.reply||'Разговор завершён.')}</p>`:''}</section>`;}
async function prepareVoice(){
 if(voiceBusy)return;const user=identity,selected=track,key=requestKey();voiceBusy=true;voiceError='';changed();
 try{let data=await api('/academy/development');if(identity!==user||track!==selected)return;let sessions=Array.isArray(data.sessions)?data.sessions:[];const matches=session=>selected==='booking'?session.clientType?.startsWith('booking-'):!session.clientType?.startsWith('booking-');let current=sessions.find(session=>session.status==='ACTIVE'&&matches(session));if(!current){const created=await api('/academy/sales-training',{method:'POST',body:{clientType:selected==='booking'?'booking-approach':'budget'}});if(identity!==user||track!==selected)return;data=await api('/academy/development');sessions=Array.isArray(data.sessions)?data.sessions:[];current=sessions.find(session=>session.id===created.id);if(!current)throw Error('Не удалось открыть сохранённый разговор. Повторите подключение.');}if(identity!==user||track!==selected)return;voiceSessions.set(key,current);voiceSession=current;}
 catch(failure){if(identity===user&&track===selected)voiceError=failure.message;}
 finally{if(identity===user&&track===selected){voiceBusy=false;changed();}}
}
function lines(text){return String(text||'').split(/\n+/).filter(Boolean).map(line=>`<p>${esc(line)}</p>`).join('');}
function sourceView(source){return `<li>${typeof source.url==='string'&&source.url.startsWith('https://')?`<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.label)}</a>`:esc(source.label)} · ${esc(source.id)}</li>`;}
function answerView(item){if(!item)return '';const a=item.answer;return `<article class="notice section"><h3>${esc(item.question)}</h3>${item.cached?'<p class="small">Ранее сохранённый ответ по этим же результатам.</p>':''}${lines(a.answer)}${a.observations.length?`<h4>По сохранённым работам</h4><ul>${a.observations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<h4>Что сделать</h4><ol>${a.steps.map(x=>`<li>${esc(x)}</li>`).join('')}</ol>${a.exercise?`<h4>Практическое упражнение</h4>${lines(a.exercise)}`:''}${a.limitations.length?`<details><summary>Границы разбора</summary><ul>${a.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details>`:''}${item.sources?.length?`<details><summary>На чём основан ответ</summary><ul>${item.sources.map(sourceView).join('')}</ul></details>`:''}</article>`;}
export function academyMentorScreen(){
 const offline=snapshot?._offline||globalThis.navigator?.onLine===false;
 const examples=track==='booking'?['Как спокойно объяснить условия съёмки?','Разбери мои повторяющиеся ошибки в диалогах.']:['Почему мои фотографии нерезкие и что проверить?','Разбери мои слабые места по последним работам.','Как снять семью в жёстком полуденном свете?'];
 return `<section class="academy-panel section" id="academyMentor"><h2>Спросить AI-наставника</h2><p>${track==='booking'?'Вопросы по приглашению, записи, условиям и возражениям гостей.':'Вопросы по технике, свету, композиции, позированию и обработке.'} Личный разбор опирается на ваши сохранённые оценки и задания.</p>${snapshot?`<p class="small">Доступно сохранённых разборов: ${snapshot.sources.length}. ${esc(snapshot.limitation)}</p>${snapshot.diagnostic?'<p>Пока мало оценённых работ: наставник объяснит общие принципы и предложит диагностику.</p>':''}`:''}${offline?'<p class="notice">Без сети доступны ранее загруженные результаты и ответы. Новый вопрос можно отправить после подключения.</p>':''}${snapshot&&!snapshot.configured?'<p class="notice">Новый ответ сейчас недоступен: AI-провайдер не настроен. Сохранённые результаты доступны.</p>':''}${voicePanel()}<div class="row wrap">${examples.map(q=>`<button class="btn ghost" data-mentor-question="${esc(q)}" ${isPending()?'disabled':''}>${esc(q)}</button>`).join('')}</div><form id="academyMentorForm" data-draft-scope="${esc(String(identity))}:${track}" class="form-grid section"><label class="field">Ваш вопрос<textarea class="input" name="question" rows="3" minlength="3" maxlength="2000" required placeholder="Что улучшить в моих работах и как это отработать?">${esc(request?.question||'')}</textarea></label><div class="row wrap"><button class="btn primary" type="submit" ${isPending()||offline||!snapshot?.configured?'disabled':''}>${isPending()?'Наставник готовит ответ…':'Получить подробный ответ'}</button>${isPending()?'<button class="btn ghost" type="button" data-mentor-cancel>Прекратить ожидание</button>':''}</div><p role="status" aria-live="polite">${isPending()?'Подробный ответ готовится по урокам и сохранённым разборам. Скорость зависит от соединения и AI-провайдера.':''}</p><p role="alert">${esc(error)}</p></form>${answerView(answer)}${snapshot?.sources.length?`<details class="section"><summary>Последние сохранённые результаты</summary>${snapshot.sources.slice(0,5).map(source=>`<article class="notice section"><strong>${esc(source.label)}</strong>${typeof source.summary.score==='number'?`<p>Оценка: ${source.summary.score}/100</p>`:''}${(source.summary.issues||source.summary.errors||[]).slice(0,3).map(issue=>`<p>${esc(issue)}</p>`).join('')}</article>`).join('')}</details>`:''}${snapshot?.history?.length>1?`<details class="section"><summary>Предыдущие ответы</summary>${snapshot.history.filter(item=>item.requestId!==answer?.requestId&&item.track===track).slice(0,4).map(answerView).join('')}</details>`:''}</section>`;
}
document.addEventListener('click',event=>{
 const button=event.target.closest('[data-mentor-question],[data-mentor-cancel],[data-mentor-voice]');if(!button)return;
 if(button.hasAttribute('data-mentor-voice')){void prepareVoice();return;}
 if(button.hasAttribute('data-mentor-cancel')){if(request){request.pending=false;request.detached=true;}error='Ожидание прекращено. Если сервер завершит ответ, он сохранится в истории. Повтор того же вопроса использует тот же запрос.';changed();return;}
 const input=document.querySelector('#academyMentorForm textarea');if(input){input.value=button.dataset.mentorQuestion;input.focus();captureForms();}
});
document.addEventListener('submit',async event=>{
 if(event.target.id!=='academyMentorForm')return;event.preventDefault();if(isPending())return;
 const form=event.target,question=String(new FormData(form).get('question')||'').trim();
 if(question.length<3){error='Напишите вопрос от трёх символов.';changed();return;}
 const user=identity,selected=track,key=requestKey();
 const previous=requests.get(key),active=previous?.question===question&&!previous.completed?previous:{question,id:newId()};
 if(active.inFlight){error='Предыдущий запрос ещё выполняется на сервере. Его ответ сохранится в истории.';changed();return;}
 active.pending=true;active.inFlight=true;active.detached=false;requests.set(key,active);request=active;error='';changed();
 try{
  const result=await api('/academy/mentor/ask?track='+encodeURIComponent(selected),{method:'POST',body:{question,requestId:active.id},timeoutMs:85000});
  active.completed=true;if(identity!==user||track!==selected||requests.get(key)!==active)return;
  answer=result;if(snapshot){snapshot.history=[result,...(snapshot.history||[]).filter(item=>item.requestId!==result.requestId)].slice(0,8);}
  error='';clearForm(form);
  // A GET updates the existing per-user offline snapshot with the saved answer.
  api('/academy/mentor?track='+encodeURIComponent(selected)).then(saved=>{if(identity===user&&track===selected){snapshot={...saved,sources:Array.isArray(saved.sources)?saved.sources:[],history:Array.isArray(saved.history)?saved.history:[],skills:Array.isArray(saved.skills)?saved.skills:[]};changed();}}).catch(()=>{});
 }catch(failure){if(identity===user&&track===selected&&requests.get(key)===active)error=failure.message;}
 finally{active.pending=false;active.inFlight=false;if(identity===user&&track===selected&&requests.get(key)===active){request=active;changed();}}
});

document.addEventListener('pb-voice-turn-saved',async event=>{if(!voiceSession||voiceSession.id!==event.detail?.id)return;const user=identity,selected=track,key=requestKey();try{const data=await api('/academy/development');if(identity!==user||track!==selected)return;const current=(data.sessions||[]).find(item=>item.id===event.detail.id);if(current){voiceSession=current;voiceSessions.set(key,current);changed();}}catch(failure){if(identity===user&&track===selected){voiceError=failure.message;changed();}}});
