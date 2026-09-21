import {api,ApiError} from './api.js';
import {esc} from './domain.js';

const panel=document.createElement('dialog');panel.className='practice-dialog';
panel.setAttribute('aria-labelledby','practiceTitle');document.body.append(panel);
let current=null,pollTimer=null,version=0,busy=false,lastFocus=null;
const urls=new Set();
const labels={ACTIVE:'В работе',PENDING_REVIEW:'На проверке',COMPLETED:'Практика принята'};
function cleanup(){version++;for(const url of urls)URL.revokeObjectURL(url);urls.clear();}
function close(){cleanup();clearInterval(pollTimer);panel.close();lastFocus?.focus?.();document.dispatchEvent(new CustomEvent('pb-practice-updated'));}
function shell(title,html){cleanup();panel.innerHTML=`<header class="practice-head"><button data-practice-home aria-label="Назад">‹</button><h2 id="practiceTitle">${esc(title)}</h2><button data-practice-close aria-label="Закрыть">×</button></header><div class="practice-body"><p class="practice-error" role="alert" hidden></p>${html}</div>`;if(!panel.open){lastFocus=document.activeElement;panel.showModal();}}
function error(e){const el=panel.querySelector('.practice-error');if(el){el.hidden=false;el.textContent=e.message||'Не удалось загрузить практику.';}}
async function photoFetch(path,options={}){
 const initData=window.Telegram?.WebApp?.initData;if(!initData)throw new ApiError('Откройте приложение через Telegram.',401);
 const response=await fetch(`/api/miniapp${path}`,{...options,credentials:'omit',cache:'no-store',redirect:'error',headers:{'X-Telegram-Init-Data':initData}});
 if(!response.ok){const d=await response.json().catch(()=>null);throw new ApiError(d?.error||'Фото временно недоступно.',response.status);}
 return response;
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
 if(a.status!=='completed')return '<p class="practice-feedback">Работу проверит владелец. Результат появится здесь.</p>';
 const r=a.review;
 return `<div class="practice-feedback"><h3>Разбор: ${esc(r.score)}/100</h3>${r.strengths?.length?`<p>${esc(r.strengths.join(' · '))}</p>`:''}${r.issues?.length?`<ul>${r.issues.map(i=>`<li>${esc(i)}</li>`).join('')}</ul>`:''}<p>${esc(r.next_action)}</p></div>`;
}
function renderAssignment(data){
 current=data;
 const editable=data.own&&data.status==='ACTIVE';
 shell(data.title,`<p>${esc(data.userName)} · <strong data-practice-status>${esc(labels[data.status]||data.status)}</strong></p>
  <p>Загружено ${data.shots.filter(s=>s.uploaded).length}/5. Для каждого кадра показаны референс и задание.</p>
  ${data.status==='PENDING_REVIEW'?`<p>${data.reviewSource==='AI_PENDING'?'ИИ проверяет фотографии…':'Ожидаем решения владельца.'} Результат обновится автоматически.</p>`:''}
  ${analysisMarkup(data)}<div class="practice-shots">${data.shots.map(s=>`<article class="practice-shot"><h3>Кадр ${s.index}/5</h3><p>${esc(s.instruction)}</p>
  <div class="practice-pair"><figure><img data-practice-photo="${esc(s.reference)}" alt="Учебный референс ${s.index}"><figcaption>Референс</figcaption></figure>
  ${s.photo?`<figure><img data-practice-photo="${esc(s.photo)}" alt="Работа фотографа ${s.index}"><figcaption>Ваш кадр сохранён</figcaption></figure>`:'<p class="practice-missing">Кадр ещё не загружен</p>'}</div>
  ${editable&&!s.uploaded?`<label class="practice-upload">Загрузить кадр ${s.index}<input type="file" accept="image/jpeg,image/png,image/webp" data-practice-upload="${s.index}"></label>`:''}</article>`).join('')}</div>
  ${editable?'<p>Выберите фото из галереи или камеры. Приложение подготовит JPEG до 8 МБ. После пятого кадра набор автоматически отправится на проверку.</p>':''}
  ${data.canReview?`<form id="practiceReview"><h3>Проверка владельца</h3><p>Отметьте кадры только для пересъёмки.</p><div class="practice-checks">${data.shots.map(s=>`<label><input type="checkbox" name="index" value="${s.index}"> Кадр ${s.index}</label>`).join('')}</div><label>Комментарий<textarea name="comment" maxlength="2000" rows="3"></textarea></label><div class="practice-actions"><button type="submit" name="decision" value="accept">Принять 5/5</button><button type="submit" name="decision" value="revision">Вернуть на пересъёмку</button></div></form>`:''}
  <button class="practice-refresh" data-practice-refresh>Обновить</button>`);
 loadPhotos();
}
async function showAssignment(id){const data=await api(`/academy/practice/${id}`);renderAssignment(data);}
async function home(){
 current=null;
 const data=await api('/academy/practice');
 shell('Практика Академии',`${data.assignment?`<button class="practice-refresh" data-practice-open="${data.assignment.id}">Продолжить: ${esc(data.assignment.title)} · ${esc(labels[data.assignment.status])}</button>`:data.todayDone?'<p>Практика принята. Новый набор откроется завтра.</p>':!data.ready?'<p>Завершите четыре урока текущего блока, чтобы открыть практику.</p>':`<p>Выберите категорию и повторите пять кадров. Задание и результаты останутся в приложении.</p><div class="practice-menu">${data.categories.map(c=>`<button data-practice-start="${esc(c.slug)}">${esc(c.title)}</button>`).join('')}</div>`}
 ${data.queue.length?`<h3>Проверить работы команды</h3><div class="practice-menu">${data.queue.map(a=>`<button data-practice-open="${a.id}">${esc(a.name)} · ${esc(a.title)}</button>`).join('')}</div>`:''}
 ${data.history.length?`<h3>Мои задания</h3><div class="practice-menu">${data.history.map(a=>`<button data-practice-open="${a.id}">${esc(a.title)} · ${esc(labels[a.status]||a.status)}</button>`).join('')}</div>`:''}`);
}
export async function openPractice(id=null){
 shell('Практика Академии','<p>Загружаем задание…</p>');
 try{if(id)await showAssignment(id);else await home();}catch(e){error(e);}
 clearInterval(pollTimer);pollTimer=setInterval(async()=>{
  if(!panel.open||!current||current.status!=='PENDING_REVIEW'||busy||document.hidden)return;
  try{const data=await api(`/academy/practice/${current.id}`);if(panel.open&&current?.id===data.id&&JSON.stringify(data)!==JSON.stringify(current))renderAssignment(data);}catch(e){error(e);}
 },5000);
}
async function preparePhoto(file){
 if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type))throw new Error('Выберите JPEG, PNG или WebP.');
 if(file.size>25*1024*1024)throw new Error('Исходное фото больше 25 МБ. Выберите уменьшенную копию.');
 const image=await createImageBitmap(file);
 try{
  const scale=Math.min(1,2048/Math.max(image.width,image.height));
  const canvas=document.createElement('canvas');canvas.width=Math.round(image.width*scale);canvas.height=Math.round(image.height*scale);
  const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',0.9));
  if(!blob||blob.size>8*1024*1024)throw new Error('Не удалось подготовить фото до 8 МБ.');
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
  if(b.hasAttribute('data-practice-home'))return await home();
  if(b.dataset.practiceOpen)return await showAssignment(Number(b.dataset.practiceOpen));
  if(b.hasAttribute('data-practice-refresh'))return current?await showAssignment(current.id):await home();
  if(b.dataset.practiceStart){busy=true;b.disabled=true;renderAssignment(await api('/academy/practice/start',{method:'POST',body:{category:b.dataset.practiceStart}}));}
 }catch(err){error(err);b.disabled=false;}finally{busy=false;}
});
panel.addEventListener('submit',async e=>{
 if(e.target.id!=='practiceReview')return;e.preventDefault();if(busy)return;
 const decision=e.submitter?.value,indexes=[...e.target.querySelectorAll('input:checked')].map(i=>Number(i.value)),comment=e.target.elements.comment.value.trim();
 if(decision==='revision'&&(!indexes.length||!comment))return error(new Error('Выберите кадры и напишите, что исправить.'));
 busy=true;
 try{renderAssignment(await api(`/academy/practice/${current.id}/review`,{method:'POST',body:{decision,indexes,comment}}));}
 catch(err){error(err);}finally{busy=false;}
});
panel.addEventListener('cancel',e=>{e.preventDefault();close();});
