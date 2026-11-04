import {api} from './api.js';

const node = document.querySelector('#hr-content');
const status = document.querySelector('#hr-status');
const labels = {title:'Название вакансии',duties:'Обязанности',pay:'Оплата',payout:'Сроки выплат',
  schedule:'График и часы',location:'Место работы',housing:'Жильё',travel:'Проезд',
  training:'Обучение и опыт',equipment:'Оборудование',contract:'Оформление',bonuses:'Бонусы',meals:'Питание',start:'Начало работы',seasonality:'Сезонность',workplace:'Характер работы',requirements:'Навыки',portfolio:'Портфолио'};
const states = {ACTIVE:'Общается ассистент',REVIEW:'Нужен ответ менеджера',READY:'Готов к следующему шагу',
  HUMAN:'Переписка у менеджера',STOPPED:'Кандидат отказался от общения'};
let data;
function element(tag, text, className){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(className)e.className=className;return e;}
function button(text, action){const b=element('button',text,'btn ghost');b.type='button';b.addEventListener('click',()=>run(b,action));return b;}
async function run(control, action){control.disabled=true;try{await action();}catch(error){status.textContent=error.message;}finally{control.disabled=false;}}
async function load(){
  data=await api('/hr/avito');
  status.textContent=data.configured
    ? (data.workerReady?'Подключение настроено. ':'Фоновый обработчик пока не готов. ')+(data.lastCheck?'Последняя проверка: '+new Date(data.lastCheck*1000).toLocaleString('ru-RU'): 'Успешная проверка Авито ещё не подтверждена.')
    : 'Ключи подключения Авито на сервере не настроены. Кабинет и условия доступны.';
  if(data.paused)status.textContent+=' Автообработка приостановлена: '+data.pauseReason+'.';
  node.replaceChildren();
  renderBind();
  node.append(element('h2','Условия вакансий'),element('p','Ассистент использует только подтверждённые условия конкретной вакансии. Сохранение здесь не меняет объявление на Авито.'));
  for(const vacancy of data.vacancies)renderVacancy(vacancy);
  if(!data.vacancies.length)node.append(element('p','Подключённых вакансий пока нет. Нужно связать действующие объявления с интеграцией.'));
  node.append(element('h2','Кандидаты'),element('p',data.note));
  for(const candidate of data.candidates)renderCandidate(candidate);
  if(!data.candidates.length)node.append(element('p','Обработанных диалогов пока нет.'));
}
function renderBind(){
  const form=element('form',null,'panel section');form.append(element('h2','Подключить действующую вакансию'));
  const itemLabel=element('label','Номер объявления Авито ','field');const item=element('input',null,'input');item.type='text';item.inputMode='numeric';item.pattern='[0-9]+';item.required=true;itemLabel.append(item);
  const role=element('select',null,'input');for(const [value,text] of [['PHOTOGRAPHER','Фотограф'],['MANAGER','Менеджер по записи']]){const option=element('option',text);option.value=value;role.append(option);}
  const submit=element('button','Подключить','btn primary');submit.type='submit';form.append(itemLabel,role,submit,element('p','Проверим, что действующее объявление принадлежит подключённому аккаунту. Новое объявление не публикуется. После подключения заполните условия.'));
  form.addEventListener('submit',event=>{event.preventDefault();run(submit,async()=>{const itemId=Number(item.value);if(!Number.isSafeInteger(itemId)||itemId<=0)throw new Error('Введите корректный номер объявления.');await api('/hr/avito/vacancies/bind',{method:'POST',body:{itemId,role:role.value}});await load();status.textContent='Вакансия подключена. Заполните и подтвердите условия.';});});
  node.append(form);
}
function renderVacancy(v){
  const section=element('details',null,'panel section');section.append(element('summary',(v.payload.title||v.code)+' · '+(v.confirmed?'условия подтверждены':'нужно заполнить условия')));
  const form=element('form',null,'form-grid');
  for(const field of data.fields){const label=element('label',labels[field]||field,'field');const input=element('textarea',null,'input');input.name=field;input.maxLength=500;input.value=v.payload[field]||'';label.append(input);form.append(label);}
  const confirm=element('input');confirm.type='checkbox';confirm.name='confirmed';confirm.checked=v.confirmed;
  const confirmLabel=element('label','Подтверждаю актуальность условий для ответов кандидатам ');confirmLabel.append(confirm);form.append(confirmLabel);
  const save=element('button','Сохранить условия','btn primary');save.type='submit';form.append(save);
  form.addEventListener('submit',event=>{event.preventDefault();run(save,async()=>{const fields=new FormData(form),payload={};for(const key of data.fields)payload[key]=fields.get(key);await api('/hr/avito/vacancies/'+encodeURIComponent(v.code)+'/terms',{method:'PUT',body:{revision:v.revision,payload,confirmed:confirm.checked}});await load();status.textContent='Условия сохранены.';});});
  section.append(form);
  const preview=element('textarea',null,'input');preview.placeholder='Тестовый вопрос кандидата';preview.maxLength=2000;
  const output=element('p');
  section.append(element('h3','Проверить ответ без отправки'),preview,button('Проверить',async()=>{const result=await api('/hr/avito/preview',{method:'POST',body:{code:v.code,text:preview.value}});output.textContent=result.answer+' · '+(result.needsManager?'Требуется менеджер':'Ассистент продолжит разговор')+' · Сообщение не отправлялось.';}),output);
  node.append(section);
}
function renderCandidate(c){
  const section=element('section',null,'panel section');section.append(element('h3',c.profile.name||'Кандидат из Авито'),element('p',(states[c.status]||c.status)+' · '+c.vacancy_code));
  for(const [key,label] of Object.entries(data.profileLabels))section.append(element('p',label+': '+(c.profile[key]||'Не выяснено')));
  const transcript=element('details');transcript.append(element('summary','История диалога'));
  for(const message of c.transcript)transcript.append(element('p',(message.direction==='in'?'Кандидат: ':'Компания: ')+message.text));
  section.append(transcript);
  if(c.uncertain_send)section.append(element('p','Результат последней отправки неизвестен. Этот диалог заблокирован от повторной отправки; проверьте сообщения в Авито.'));
  if(c.status!=='STOPPED'){
    const action=c.status==='HUMAN'?'resume':'pause';section.append(button(action==='pause'?'Взять переписку на себя':'Вернуть ассистенту',async()=>{await api('/hr/avito/chats/'+encodeURIComponent(c.chat_id)+'/control',{method:'POST',body:{action}});await load();}));
  }
  section.append(element('p','Ответить кандидату можно в чате Авито. Сообщения человека автоматически передают диалог менеджеру.'));
  node.append(section);
}
document.querySelector('#hr-refresh').addEventListener('click',event=>run(event.currentTarget,load));
load().catch(error=>{status.textContent=error.message+' Сначала войдите в Фотобосс под владельцем или администратором.';});
