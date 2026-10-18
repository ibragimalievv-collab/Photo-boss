import {api} from './api.js';
import {esc} from './domain.js';

const panel=document.createElement('dialog');panel.className='practice-dialog';
panel.setAttribute('aria-labelledby','practiceTitle');document.body.append(panel);
let current=null,pollTimer=null,version=0,busy=false,lastFocus=null;
const urls=new Set();
const labels={ACTIVE:'В работе',PENDING_REVIEW:'На проверке',COMPLETED:'Практика принята'};
const criterionLabels={focus:['Фокус и техника',20],light:['Свет',15],composition:['Композиция',15],pose:['Позирование',20],emotion:['Выразительность',15],variety:['Разнообразие',15]};
const evidenceLabels={ACHIEVED:'Выполнено',PARTIAL:'Частично',NOT_ACHIEVED:'Нужно исправить',NOT_ASSESSABLE:'Недостаточно данных',FIXED:'Исправлено',STILL_PRESENT:'Ошибка сохраняется',LOW:'Низкая',MEDIUM:'Средняя',HIGH:'Высокая'};
function statusLabel(data){
 if(data.status==='ACTIVE'&&data.reviewSource==='STORAGE_RETRY')return 'Нужна повторная загрузка';
 if(data.status==='ACTIVE'&&data.reviewSource==='AI')return 'Нужна пересъёмка';
 if(data.status==='PENDING_REVIEW'&&data.reviewSource==='AI_FAILED')return 'Проверка прервалась';
 if(data.status==='PENDING_REVIEW'&&data.reviewSource==='AI_UNCERTAIN')return 'Нужно уточнить кадры';
 return labels[data.status]||data.status;
}
function reviewSummary(data){
 const a=data.analysis||{},r=a.review;
 if(r?.next_action)return r.next_action;
 if(a.message)return a.message.replaceAll('Ваш текст сохранён','Фотографии сохранены');
 if(data.reviewSource==='STORAGE_RETRY')return 'Откройте задание и загрузите только отмеченные кадры.';
 return '';
}
function cleanup(){version++;for(const url of urls)URL.revokeObjectURL(url);urls.clear();}
function close(){cleanup();clearInterval(pollTimer);panel.close();lastFocus?.focus?.();document.dispatchEvent(new CustomEvent('pb-practice-updated'));}
function shell(title,html){cleanup();panel.innerHTML=`<header class="practice-head"><button data-practice-home aria-label="Назад">‹</button><h2 id="practiceTitle">${esc(title)}</h2><button data-practice-close aria-label="Закрыть">×</button></header><div class="practice-body"><p class="practice-error" role="alert" hidden></p>${html}</div>`;if(!panel.open){lastFocus=document.activeElement;panel.showModal();}}
function error(e){const el=panel.querySelector('.practice-error');if(el){el.hidden=false;el.textContent=e.message||'Не удалось загрузить практику.';}}
async function photoFetch(path,options={}){
 return api(path,{...options,responseType:'response',timeoutMs:90000});
}
async function loadPhotos(){
 const ticket=version;
 for(const img of panel.querySelectorAll('[data-practice-photo]')){
  try{const response=await photoFetch(img.dataset.practicePhoto);const blob=await response.blob();if(ticket!==version)return;const url=URL.createObjectURL(blob);urls.add(url);img.src=url;}
  catch{if(ticket===version){img.alt='Фото не загрузилось. Нажмите «Обновить».';}}
 }
}
function analysisMarkup(data){
 const a=data.analysis;if(!a)return '';
 if(a.status==='owner')return `<div class="practice-feedback"><strong>Решение владельца</strong><p>${esc(a.comment||'Набор принят.')}</p></div>`;
 if(a.status==='storage_unavailable')return `<p class="practice-feedback">Не удалось прочитать ${esc((a.unavailable_indexes||[]).map(i=>`кадр ${i}`).join(', ')||'один из кадров')} из хранилища. Остальные фотографии сохранены; загрузите только отмеченные кадры заново.</p>`;
 if(a.status==='processing')return `<p class="practice-feedback">ИИ проверяет сохранённые фотографии. Попытка ${(a.attempts||0)+1}/3. Повторно отправлять фото не нужно.</p>`;
 if(a.status!=='completed')return `<p class="practice-feedback">${esc(reviewSummary(data)||'AI-проверка временно недоступна. Фотографии сохранены; повторите проверку.')}${a.nextRetryAt?' Следующая попытка запланирована автоматически; фотографии повторно загружать не нужно.':''}</p>`;
 const r=a.review;
 const evidence=(items,key,title)=>items?.length?`<h4>${title}</h4><ul>${items.map(x=>`<li><strong>${esc(x[key])}: ${esc(evidenceLabels[x.status]||x.status)}</strong>${x.frame_indexes?.length?` · кадры ${esc(x.frame_indexes.join(', '))}`:''}<p>${esc(x.evidence)}</p><p>${esc(x.action)}</p></li>`).join('')}</ul>`:'';
 return `<div class="practice-feedback"><h3>Разбор: ${esc(r.score)}/100</h3>
 ${r.criteria?`<dl>${Object.entries(criterionLabels).map(([key,[label,max]])=>`<dt>${label}</dt><dd>${esc(r.criteria[key])}/${max}</dd>`).join('')}</dl>`:''}
 ${r.strengths?.length?`<h4>Что получилось</h4><p>${esc(r.strengths.join(' · '))}</p>`:''}
 ${r.issues?.length?`<h4>Что исправить</h4><ul>${r.issues.map(i=>`<li>${esc(i)}</li>`).join('')}</ul>`:''}
 ${r.duplicate_pairs?.length?`<p><strong>Похожие ракурсы:</strong> ${esc(r.duplicate_pairs.map(pair=>pair.join('–')).join(', '))}</p>`:''}
 ${evidence(r.goal_results,'criterion','Результат личного задания')}${evidence(r.correction_checks,'previous_issue','Исправление прежних ошибок')}
 ${r.frames?.length?`<h4>Разбор каждого кадра</h4>${r.frames.map(f=>`<section><h4>Кадр ${esc(f.index)}</h4><p>${esc(f.observation)}</p><p><strong>Следующий шаг:</strong> ${esc(f.action)}</p><p class="small">Уверенность: ${esc(evidenceLabels[f.confidence]||f.confidence)}. ${esc(f.uncertainty)}</p></section>`).join('')}`:''}
 ${r.limitations?.length?`<h4>Ограничения проверки</h4><ul>${r.limitations.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}
 ${a.imageEvidence?.some(x=>x.reencoded)?`<p class="small">ИИ проверял отдельные JPEG-копии до 2048 пикселей. Оригиналы сохранены; оценка пиксельной резкости исходника 1:1 не выполнялась.</p><ul class="small">${a.imageEvidence.map(x=>`<li>Кадр ${esc(x.index)}: исходник ${esc(x.original.width)}×${esc(x.original.height)} → анализ ${esc(x.analysis.width)}×${esc(x.analysis.height)}</li>`).join('')}</ul>`:''}
 <p><strong>Следующее действие:</strong> ${esc(r.next_action)}</p></div>`;
}
function renderAssignment(data){
 current=data;
 const editable=data.own&&data.status==='ACTIVE';
 shell(data.title,`<p>${esc(data.userName)} · <strong data-practice-status>${esc(statusLabel(data))}</strong></p>
  <p>Загружено ${data.shots.filter(s=>s.uploaded).length}/5. Для каждого кадра показаны референс и задание.</p>
  ${data.status==='PENDING_REVIEW'?`<p>${['AI_PENDING','AI_PROCESSING'].includes(data.reviewSource)?'ИИ проверяет фотографии…':data.analysis?.review?.decision==='MANUAL_REVIEW'?'ИИ не смог уверенно оценить кадры. Можно повторить проверку или переснять спорные кадры.':'AI-проверку нужно повторить.'} Результат обновится автоматически.</p>`:''}
  ${editable&&data.reviewSource==='AI'?`<p class="practice-feedback">Проверка уже завершена: нужно переснять ${esc((data.analysis?.review?.reshoot_indexes||[]).map(i=>`кадр ${i}`).join(', '))}. Остальные кадры сохранены. Подробный разбор — ниже.</p>`:''}
  ${data.personalInstructions?.length?`<section class="practice-feedback"><h3>Ваше AI-задание</h3><ol>${data.personalInstructions.map(x=>`<li>${esc(x)}</li>`).join('')}</ol>${data.successCriteria?.length?`<h4>Критерии успеха</h4><ul>${data.successCriteria.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<p class="small">Условия закреплены за этой практикой и сохраняются при пересъёмке.</p></section>`:''}${analysisMarkup(data)}<div class="practice-shots">${data.shots.map(s=>`<article class="practice-shot"><h3>Кадр ${s.index}/5</h3><p>${esc(s.instruction)}</p>
  <div class="practice-pair"><figure><img data-practice-photo="${esc(s.reference)}" alt="Учебный референс ${s.index}"><figcaption>Референс</figcaption></figure>
  ${s.photo?`<figure><img data-practice-photo="${esc(s.photo)}" alt="Работа фотографа ${s.index}"><figcaption>Ваш кадр сохранён</figcaption></figure>`:'<p class="practice-missing">Кадр ещё не загружен</p>'}</div>
  ${editable&&!s.uploaded?`<label class="practice-upload">Загрузить кадр ${s.index}<input type="file" accept="image/jpeg,image/png,image/webp" data-practice-upload="${s.index}"></label>`:''}</article>`).join('')}</div>
  ${editable?'<p>Загрузите исходный JPEG до 8 МБ: размер и EXIF сохраняются. PNG и WebP преобразуются в JPEG без уменьшения; их EXIF не сохраняется. Фото больше 8 МБ нужно уменьшить самостоятельно. После пятого кадра начнётся проверка.</p>':''}
  ${data.canRetry?`<button class="btn primary" data-practice-retry>Повторить AI-проверку</button>`:''}
  ${data.canRetake?'<button class="btn" data-practice-retake>Переснять спорные кадры</button><p>Указанные ИИ кадры откроются для замены. Если номера не указаны — все пять. Прежние работы останутся в истории.</p>':''}
  <button class="practice-refresh" data-practice-refresh>Обновить</button>`);
 loadPhotos();
}
async function showAssignment(id){const data=await api(`/academy/practice/${id}`);renderAssignment(data);}
async function home(){
 current=null;
 const data=await api('/academy/practice');
 shell('Практика Академии',`${data.assignment?`<button class="practice-refresh" data-practice-open="${data.assignment.id}">Продолжить: ${esc(data.assignment.title)} · ${esc(statusLabel(data.assignment))}</button>${reviewSummary(data.assignment)?`<p class="practice-feedback">${esc(reviewSummary(data.assignment))}</p>`:''}`:data.todayDone?'<p>Практика принята. Новый набор откроется завтра.</p>':!data.ready?'<p>Завершите четыре урока текущего блока, чтобы открыть практику.</p>':`<p>AI-наставник назначает практику по вашим результатам и текущему блоку.</p><button class="btn primary" data-practice-plan>Открыть персональное AI-задание</button>`}
 ${data.queue.length?`<h3>Проверить работы команды</h3><div class="practice-menu">${data.queue.map(a=>`<button data-practice-open="${a.id}">${esc(a.name)} · ${esc(a.title)}</button>`).join('')}</div>`:''}
 ${data.history.length?`<h3>Мои задания</h3><div class="practice-menu">${data.history.map(a=>`<button data-practice-open="${a.id}">${esc(a.title)} · ${esc(statusLabel(a))}${a.score!==null&&a.score!==undefined?` · ${esc(a.score)}/100`:''}</button>`).join('')}</div>`:''}`);
}
export async function openPractice(id=null,category=null){
 shell('Практика Академии','<p>Загружаем задание…</p>');
 try{if(id)await showAssignment(id);else if(category)renderAssignment(await api('/academy/practice/start',{method:'POST',body:{category}}));else await home();}catch(e){error(e);}
 clearInterval(pollTimer);pollTimer=setInterval(async()=>{
  if(!panel.open||!current||current.status!=='PENDING_REVIEW'||busy||document.hidden)return;
  try{const data=await api(`/academy/practice/${current.id}`);if(panel.open&&current?.id===data.id&&JSON.stringify(data)!==JSON.stringify(current))renderAssignment(data);}catch(e){error(e);}
 },5000);
}
async function preparePhoto(file){
 if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type))throw new Error('Выберите JPEG, PNG или WebP.');
 if(file.size>25*1024*1024)throw new Error('Исходное фото больше 25 МБ. Выберите уменьшенную копию.');
 if(file.type==='image/jpeg'){
  if(file.size>8*1024*1024)throw new Error('JPEG больше 8 МБ. Сохраните отдельную копию до 8 МБ; её детализация может быть ниже.');
  // A byte copy preserves EXIF and avoids file-backed multipart transport differences.
  return new Blob([await file.arrayBuffer()],{type:'image/jpeg'});
 }
 const image=await createImageBitmap(file);
 try{
  if(image.width*image.height>32*1024*1024)throw new Error('Для PNG/WebP больше 32 Мп сохраните отдельный JPEG до 8 МБ.');
  const canvas=document.createElement('canvas');canvas.width=image.width;canvas.height=image.height;
  const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',0.9));
  if(!blob||blob.size>8*1024*1024)throw new Error('После преобразования фото больше 8 МБ. Подготовьте отдельную JPEG-копию до 8 МБ.');
  return blob;
 }finally{image.close();}
}
panel.addEventListener('change',async e=>{
 const input=e.target.closest('[data-practice-upload]');if(!input||!input.files?.[0]||busy)return;
 const aid=current.id;busy=true;input.disabled=true;
 try{const blob=await preparePhoto(input.files[0]);const body=new FormData();body.append('file',blob,'practice.jpg');
  const response=await photoFetch(`/academy/practice/${aid}/photos/${input.dataset.practiceUpload}`,{method:'POST',body});
  const data=await response.json();if(panel.open&&current?.id===aid)renderAssignment(data);
 }catch(err){error(err);input.disabled=false;input.value='';}finally{busy=false;}
});
panel.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 if(b.hasAttribute('data-practice-close'))return close();
 if(busy)return;
 try{
  if(b.hasAttribute('data-practice-retake')){busy=true;b.disabled=true;const indexes=current.analysis?.review?.reshoot_indexes;return renderAssignment(await api(`/academy/practice/${current.id}/retake`,{method:'POST',body:{indexes:indexes?.length?indexes:[1,2,3,4,5]}}));}
  if(b.hasAttribute('data-practice-retry')){await api(`/academy/practice/${current.id}/retry`,{method:'POST',body:{}});return await showAssignment(current.id);}
  if(b.hasAttribute('data-practice-plan')){close();document.dispatchEvent(new CustomEvent('pb-open-academy-coach'));return;}
  if(b.hasAttribute('data-practice-home'))return await home();
  if(b.dataset.practiceOpen)return await showAssignment(Number(b.dataset.practiceOpen));
  if(b.hasAttribute('data-practice-refresh'))return current?await showAssignment(current.id):await home();
  if(b.dataset.practiceStart){busy=true;b.disabled=true;renderAssignment(await api('/academy/practice/start',{method:'POST',body:{category:b.dataset.practiceStart}}));}
 }catch(err){error(err);b.disabled=false;}finally{busy=false;}
});
panel.addEventListener('cancel',e=>{e.preventDefault();close();});
