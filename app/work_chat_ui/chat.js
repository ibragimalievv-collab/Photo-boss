import {api,ApiError} from '/app/js/api.js';
import {esc,ROLE_NAMES} from '/app/js/domain.js';
import {initCalls,callToolbar,handleCallClick} from '/work-chat/calls.js';
import {icon,avatar} from '/work-chat/ui.js';
import {openRecorder} from '/work-chat/recorder.js';

const dialog=document.createElement('dialog');
dialog.className='pb-chat-dialog';
dialog.setAttribute('aria-labelledby','pbChatTitle');
document.body.append(dialog);
const css=document.createElement('link');css.rel='stylesheet';css.href='/work-chat/chat.css';document.head.append(css);

let me=null, people=null, current={kind:'general',peer:null,title:'Общий чат'}, timer=null, unreadTimer=null, last=0, previousFocus=null;
const drafts=new Map();
const objectUrls=new Set();
const MAX_ATTACHMENT_BYTES=20*1024*1024;
const presenceSession=crypto.randomUUID();
let presenceSequence=0,presenceTimer=null,presenceBusy=false,heartbeatBusy=false,onlinePeople=null,presenceAt=0;

function presenceMarkup(id){return `<span class="pb-chat-presence" data-presence-id="${id}">Проверяем статус…</span>`;}
function renderPresence(){
 const known=onlinePeople!==null&&Date.now()-presenceAt<45000&&navigator.onLine;
 for(const el of dialog.querySelectorAll('[data-presence-id]')){
  const online=known&&onlinePeople.has(Number(el.dataset.presenceId));
  const label=known?(online?'В сети':'Не в сети'):'Статус недоступен';
  if(el.textContent!==label)el.textContent=label;
  el.classList.toggle('is-online',Boolean(online));
 }
}
async function refreshPresence(){
 if(!me||presenceBusy||!dialog.open||document.visibilityState!=='visible')return;
 presenceBusy=true;
 try{const data=await api('/chat/presence');onlinePeople=new Set(data.online);presenceAt=Date.now();}
 catch{onlinePeople=null;}
 finally{presenceBusy=false;renderPresence();}
}
async function sendHeartbeat(online=document.visibilityState==='visible'&&navigator.onLine){
 if(!me||(online&&heartbeatBusy))return;
 const sequence=++presenceSequence;
 if(online)heartbeatBusy=true;
 try{await api('/chat/presence',{method:'POST',keepalive:!online,body:{sessionId:presenceSession,sequence,online}});}
 catch{/* Presence expires on the server if the device loses its connection. */}
 finally{if(online)heartbeatBusy=false;}
}
function startPresence(){
 if(presenceTimer)clearInterval(presenceTimer);
 sendHeartbeat();
 presenceTimer=setInterval(()=>{
  renderPresence();
  if(document.visibilityState==='visible'&&navigator.onLine){sendHeartbeat();refreshPresence();}
 },10000);
}

function clearObjectUrls(){for(const media of dialog.querySelectorAll('audio,video')){media.pause();media.removeAttribute('src');}for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.clear();}
function stopPoll(){if(timer){clearInterval(timer);timer=null;}}
function unreadBadge(n){return n>0?`<span class="pb-chat-unread">${n>99?'99+':n}</span>`:'';}
function setUnreadBadge(n){const btn=document.querySelector('[data-open-chat]');if(!btn)return;btn.innerHTML=`${icon('chat')}<span>Чат</span>${unreadBadge(Number(n)||0)}`;btn.setAttribute('aria-label',n>0?`Рабочий чат, непрочитанных: ${n}`:'Рабочий чат');}
async function refreshUnread(){if(!me)return;try{const data=await api('/chat/unread');setUnreadBadge(data.total||0);}catch(e){if(e.status===428)setUnreadBadge(0);}}
function startUnreadPoll(){if(unreadTimer)clearInterval(unreadTimer);refreshUnread();unreadTimer=setInterval(()=>{if(document.visibilityState==='visible')refreshUnread();},10000);}
function close(){stopPoll();clearObjectUrls();dialog.close();previousFocus?.focus?.();}
function shell(title,html){
 clearObjectUrls();
 const thread=['general','peer','owner-view'].includes(current.kind), split=thread&&people&&current.kind!=='owner-view';
 dialog.classList.toggle('is-thread',thread);dialog.classList.toggle('has-sidebar',Boolean(split));
 dialog.innerHTML=`<div class="pb-chat-layout">${split?`<aside class="pb-chat-sidebar" aria-label="Диалоги"><div class="pb-chat-sidebar-title">Сообщения <span>PHOTO BOSS</span></div>${navigation()}</aside>`:''}<section class="pb-chat-main"><header class="pb-chat-head"><button class="pb-chat-back" data-chat="${current.kind==='home'?'close':'home'}" aria-label="${current.kind==='home'?'Закрыть чат':'Назад'}">${icon('back')}</button>${thread?avatar(title,current.peer,current.kind==='general'):''}<div class="pb-chat-heading"><h2 id="pbChatTitle">${esc(title)}</h2>${current.kind==='home'?'<span class="pb-chat-subtitle">Команда Photo Boss</span>':''}</div><button class="pb-chat-close" data-chat="close" aria-label="Закрыть">${icon('close')}</button></header><div class="pb-chat-body">${html}</div></section></div>`;
 if(!dialog.open){previousFocus=document.activeElement;dialog.showModal();}
 renderPresence();
}
function navigation(){
 const rows=people.people.map(p=>`<button class="pb-chat-person ${current.kind==='peer'&&current.peer===p.id?'is-selected':''}" data-peer="${p.id}" data-search-name="${esc(p.name+' '+roles(p.roles))}">${avatar(p.name,p.id)}<span class="pb-chat-person-copy"><strong>${esc(p.name)}</strong><small>${esc(roles(p.roles))}</small>${presenceMarkup(p.id)}</span>${unreadBadge(p.unread||0)}</button>`).join('');
 return `<div class="pb-chat-navigation" data-chat-navigation><label class="pb-chat-search">${icon('search')}<input type="search" data-chat-search placeholder="Поиск" aria-label="Поиск чатов" autocomplete="off"></label><div class="pb-chat-list"><button class="pb-chat-person featured ${current.kind==='general'?'is-selected':''}" data-chat="general" data-search-name="Общий чат команда">${avatar('',0,true)}<span class="pb-chat-person-copy"><strong>Общий чат</strong><small>Вся команда в одном месте</small></span>${unreadBadge(people.general?.unread||0)}</button><div class="pb-chat-section-title">Сотрудники</div>${rows||'<p class="pb-chat-muted">Других активных сотрудников пока нет.</p>'}<p class="pb-chat-search-empty" data-search-empty hidden>Чаты не найдены</p></div><div class="pb-chat-nav-footer">${people.ownerControl?`<button class="pb-chat-link" data-chat="owner">${icon('shield')}<span>Контроль диалогов сотрудников</span></button>`:''}<button class="pb-chat-link" data-chat="rules">Общие правила</button></div></div>`;
}
function draftKey(){return `${current.kind}:${current.peer||'general'}`;}
function updateComposer(){
 const form=dialog.querySelector('#pbChatCompose');if(!form)return;
 const field=form.elements.body,hasContent=Boolean(field.value.trim()||form.elements.file.files?.length);
 form.classList.toggle('has-content',hasContent);form.querySelector('.pb-chat-send').hidden=!hasContent;
 for(const button of form.querySelectorAll('[data-record]'))button.hidden=hasContent;
 field.style.height='auto';field.style.height=`${Math.min(field.scrollHeight,132)}px`;
 drafts.set(draftKey(),field.value);
}
function scrollBottom(){const box=dialog.querySelector('#pbChatMessages');if(box)box.scrollTop=box.scrollHeight;const jump=dialog.querySelector('[data-jump-bottom]');if(jump)jump.hidden=true;}
function showError(message){const el=dialog.querySelector('[data-chat-error]');if(el){el.textContent=message;el.hidden=false;}else shell('Рабочий чат',`<p class="pb-chat-error" role="alert">${esc(message)}</p><button class="pb-chat-btn" data-chat="home">Повторить</button>`);}
function roles(list){return (list||[]).map(r=>ROLE_NAMES[r]||r).join(' · ');}
function time(v){try{return new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}).format(new Date(v));}catch{return '';}}
function fileSize(bytes){if(!Number.isFinite(bytes))return '';if(bytes<1024)return `${bytes} Б`;if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} КБ`;return `${(bytes/1024/1024).toFixed(1)} МБ`;}

async function authFetch(path,{method='GET',body,timeout=60000}={}){
 const initData=window.Telegram?.WebApp?.initData;
 if(!initData)throw new ApiError('Откройте Photo Boss внутри Telegram.',401);
 const url=new URL(`/api/miniapp${path}`,location.origin);
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeout);
 try{
  const response=await fetch(url,{method,body,signal:controller.signal,cache:'no-store',credentials:'omit',redirect:'error',headers:{'X-Telegram-Init-Data':initData}});
  if(!response.ok){
   const data=await response.json().catch(()=>null);
   throw new ApiError(data?.error||`Запрос отклонён (${response.status}).`,response.status);
  }
  return response;
 }catch(e){if(e.name==='AbortError')throw new ApiError('Загрузка заняла слишком много времени. Повторите.');if(e instanceof ApiError)throw e;throw new ApiError('Нет соединения с сервером.');}finally{clearTimeout(timer);}
}
async function fetchAttachment(id){const response=await authFetch(`/chat/attachments/${id}`);return response.blob();}
async function downloadAttachment(id,name){
 const blob=await fetchAttachment(id);const url=URL.createObjectURL(blob);
 const a=document.createElement('a');a.href=url;a.download=name||'file';a.rel='noopener';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
async function loadImagePreview(img,id){
 try{const blob=await fetchAttachment(id);if(!img.isConnected)return;const url=URL.createObjectURL(blob);objectUrls.add(url);const box=dialog.querySelector('#pbChatMessages'),near=box&&box.scrollHeight-box.scrollTop-box.clientHeight<100;img.onload=()=>{if(near&&img.isConnected)scrollBottom();};img.src=url;img.dataset.loaded='1';}
 catch{img.alt='Фото временно недоступно';img.classList.add('failed');}
}
async function ensureRules(){
 const rules=await api('/chat/rules');
 if(rules.accepted)return true;
 shell('Правила Photo Boss',`<article class="pb-chat-rules">${esc(rules.text)}</article><p class="pb-chat-muted">Подтверждение относится ко всему рабочему пространству. Это не договор оказания услуг и не отдельное согласие на обработку персональных данных.</p><p class="pb-chat-error" data-chat-error hidden></p><button class="pb-chat-btn primary" data-chat-accept data-version="${esc(rules.version)}" data-sha="${esc(rules.sha256)}">Принять общие правила и открыть чат</button>`);
 return false;
}
async function home(){
 stopPoll();last=0;current={kind:'home',peer:null,title:'Рабочий чат'};
 try{
  if(!await ensureRules())return;
  people=await api('/chat/people');
  onlinePeople=new Set(people.people.filter(p=>p.online).map(p=>p.id));presenceAt=Date.now();
  setUnreadBadge(people.totalUnread||0);
  shell('Сообщения',navigation());
  renderPresence();sendHeartbeat();refreshPresence();
 }catch(e){showError(e.message);}
}
function attachmentMarkup(a){
 if(!a)return '';
 if(a.isAudio||a.isVideo){const tag=a.isAudio?'audio':'video';return `<div class="pb-chat-media"><button type="button" data-load-media="${a.id}"><span class="pb-chat-media-play">${icon(a.isAudio?'play':'video')}</span><span><strong>${a.isAudio?'Голосовое сообщение':'Видеосообщение'}</strong><small>${esc(fileSize(a.size))} · ${a.isAudio?'Прослушать':'Посмотреть'}</small></span></button><${tag} controls playsinline preload="none" data-media-id="${a.id}" hidden></${tag}></div>`;}
 if(a.isImage)return `<button type="button" class="pb-chat-photo" data-download-id="${a.id}" data-download-name="${esc(a.name)}" aria-label="Открыть фото"><img data-preview-id="${a.id}" alt="Фото: ${esc(a.name)}"></button><div class="pb-chat-file-caption">${esc(a.name)} · ${esc(fileSize(a.size))}</div>`;
 return `<button type="button" class="pb-chat-file" data-download-id="${a.id}" data-download-name="${esc(a.name)}"><span>${icon('file')}</span><span><strong>${esc(a.name)}</strong><small>${esc(fileSize(a.size))}</small></span></button>`;
}
function messageDay(value){const d=new Date(value);return Number.isNaN(d.getTime())?'':d.toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined});}
async function renderMessages(items,{readonly=false,own=false}={}){
 const box=dialog.querySelector('#pbChatMessages');if(!box)return;
 const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100, first=!box.querySelector('[data-message-id]');let added=0;
 for(const m of items){
  if(box.querySelector(`[data-message-id="${m.id}"]`))continue;
  box.querySelector('.pb-chat-empty')?.remove();
  const day=messageDay(m.createdAt),previous=box.querySelector('[data-message-id]:last-child');
  if(box.dataset.day!==day){const label=document.createElement('div');label.className='pb-chat-day';label.textContent=day;box.append(label);box.dataset.day=day;}
  const mine=m.senderId===me?.user?.id;
  const el=document.createElement('article');el.className='pb-chat-message '+(mine?'mine':'');el.dataset.messageId=m.id;el.dataset.sender=m.senderId;el.dataset.created=m.createdAt;
  if(previous?.dataset.sender===String(m.senderId)&&messageDay(previous.dataset.created)===day&&new Date(m.createdAt)-new Date(previous.dataset.created)<300000)el.classList.add('is-grouped');
  const clock=new Date(m.createdAt).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  el.innerHTML=`${!mine&&(current.kind==='general'||readonly)?`<div class="pb-chat-sender">${esc(m.senderName)}</div>`:''}${attachmentMarkup(m.attachment)}${m.body?`<div class="pb-chat-bubble">${esc(m.body).replace(/\n/g,'<br>')}</div>`:''}<div class="pb-chat-meta"><time datetime="${esc(m.createdAt)}">${esc(clock)}</time>${mine?`<span aria-label="Отправлено" title="Отправлено">${icon('check')}</span>`:''}</div>`;
  box.append(el);last=Math.max(last,m.id);added++;
  for(const img of el.querySelectorAll('[data-preview-id]'))loadImagePreview(img,Number(img.dataset.previewId));
 }
 if(!box.querySelector('[data-message-id]')&&!box.querySelector('.pb-chat-empty'))box.innerHTML=`<div class="pb-chat-empty">${icon('chat')}<strong>Здесь начинается разговор</strong><span>Напишите сообщение, отправьте фото<br>или позвоните.</span></div>`;
 if(added){if(first||nearBottom||own)scrollBottom();else{const jump=dialog.querySelector('[data-jump-bottom]');if(jump)jump.hidden=false;}}
 if(readonly)dialog.querySelector('.pb-chat-compose')?.remove();
}
async function poll(){
 const thread=current;if(thread.loading||!dialog.open||!['general','peer','owner-view'].includes(thread.kind))return;
 thread.loading=true;
 try{
  const path=thread.kind==='owner-view'?`/chat/owner/messages?a=${thread.a}&b=${thread.b}&after=${last}`:`/chat/messages?peer=${thread.kind==='general'?'general':thread.peer}&after=${last}`;
  const data=await api(path);if(current!==thread||!dialog.open)return;
  await renderMessages(data.messages,{readonly:thread.kind==='owner-view'});
  if(data.unread)setUnreadBadge(data.unread.total||0);
 }catch(e){if(current===thread)showError(e.message);}finally{thread.loading=false;}
}
async function openThread(kind,peer=null,title='Общий чат'){
 current={kind,peer,title};last=0;stopPoll();
 shell(title,`<div class="pb-chat-history"><div id="pbChatMessages" class="pb-chat-messages" aria-live="polite" aria-label="Сообщения"></div><button class="pb-chat-jump" data-jump-bottom aria-label="К новым сообщениям" hidden>${icon('down')}</button></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p><form class="pb-chat-compose" id="pbChatCompose"><div class="pb-chat-selected" data-selected-file hidden></div><div class="pb-chat-input-row"><button type="button" class="pb-chat-attach" data-attach aria-label="Прикрепить фото или файл" title="Фото или файл до 20 МБ">${icon('attach')}</button><input type="file" name="file" hidden><textarea name="body" maxlength="2000" rows="1" placeholder="Сообщение" aria-label="Сообщение"></textarea><button type="button" class="pb-chat-record" data-record="video" aria-label="Записать видеосообщение" title="Видеосообщение">${icon('video')}</button><button type="button" class="pb-chat-record" data-record="audio" aria-label="Записать голосовое сообщение" title="Голосовое сообщение">${icon('mic')}</button><button class="pb-chat-send" type="submit" aria-label="Отправить" title="Отправить" hidden>${icon('send')}</button></div></form>`);
 dialog.querySelector('.pb-chat-close').insertAdjacentHTML('beforebegin',callToolbar(peer,title));
 const heading=dialog.querySelector('.pb-chat-heading');
 heading.insertAdjacentHTML('beforeend',kind==='peer'?presenceMarkup(peer):'<span class="pb-chat-subtitle">Вся команда · звонки до 6 участников</span>');
 heading.querySelector('.pb-chat-presence')?.setAttribute('aria-live','polite');
 dialog.querySelector('textarea').value=drafts.get(draftKey())||'';updateComposer();
 dialog.querySelector('#pbChatMessages').addEventListener('scroll',()=>{const box=dialog.querySelector('#pbChatMessages');if(box.scrollHeight-box.scrollTop-box.clientHeight<100)dialog.querySelector('[data-jump-bottom]').hidden=true;},{passive:true});
 renderPresence();refreshPresence();
 const thread=current;await poll();if(current===thread&&dialog.open)timer=setInterval(poll,4000);
}
async function ownerThreads(){
 stopPoll();last=0;current={kind:'owner',peer:null};
 try{
  const d=await api('/chat/owner/threads');
  const rows=d.threads.map(t=>`<button class="pb-chat-thread" data-owner-a="${t.a.id}" data-owner-b="${t.b.id}" data-owner-title="${esc(t.a.name+' ↔ '+t.b.name)}"><strong>${esc(t.a.name)} ↔ ${esc(t.b.name)}</strong><small>${esc(t.last)} · ${esc(time(t.createdAt))}</small></button>`).join('');
  shell('Контроль диалогов',`<p class="pb-chat-muted">Только рабочие диалоги внутри Photo Boss. Обычный Telegram не подключён.</p><div class="pb-chat-list">${rows||'<p class="pb-chat-muted">Личных диалогов сотрудников пока нет.</p>'}</div>`);
 }catch(e){showError(e.message);}
}
async function ownerView(a,b,title){
 current={kind:'owner-view',a,b,title};last=0;stopPoll();
 shell(title,`<div class="pb-chat-readonly">Просмотр рабочего диалога</div><div id="pbChatMessages" class="pb-chat-messages" aria-live="polite"></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p>`);
 await poll();timer=setInterval(poll,4000);
}
async function showRules(){
 stopPoll();current={kind:'rules',peer:null};
 try{const r=await api('/chat/rules');shell('Общие правила',`<article class="pb-chat-rules">${esc(r.text)}</article><p class="pb-chat-muted">Редакция: ${esc(r.version)} · хранение сообщений до ${r.retentionDays} дней.</p>`);}catch(e){showError(e.message);}
}
async function uploadFile(file,caption){
 if(file.size>MAX_ATTACHMENT_BYTES)throw new ApiError('Файл больше 20 МБ.',413);
 const peer=current.kind==='general'?'general':String(current.peer);
 const form=new FormData();form.append('peerId',peer);form.append('caption',caption);form.append('file',file,file.name);
 const response=await authFetch('/chat/attachments',{method:'POST',body:form,timeout:90000});
 return response.json();
}

dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
dialog.addEventListener('change',e=>{
 if(e.target.name!=='file')return;
 const file=e.target.files?.[0];const selected=dialog.querySelector('[data-selected-file]');
 if(!selected)return;
 if(!file){selected.hidden=true;selected.textContent='';updateComposer();return;}
 selected.hidden=false;selected.innerHTML=`${icon('file')}<span>${esc(file.name)}<small>${fileSize(file.size)}</small></span><button type="button" data-remove-file aria-label="Убрать файл">${icon('close')}</button>`;updateComposer();
});
dialog.addEventListener('input',e=>{
 if(e.target.matches('[data-chat-search]')){
  const scope=e.target.closest('[data-chat-navigation]'),query=e.target.value.toLocaleLowerCase('ru').trim();let count=0;
  for(const row of scope.querySelectorAll('[data-search-name]')){row.hidden=!row.dataset.searchName.toLocaleLowerCase('ru').includes(query);if(!row.hidden)count++;}
  scope.querySelector('[data-search-empty]').hidden=count>0;return;
 }
 if(e.target.name==='body')updateComposer();
});
dialog.addEventListener('keydown',e=>{if(e.target.name==='body'&&e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!matchMedia('(pointer:coarse)').matches){e.preventDefault();e.target.form.requestSubmit();}});
dialog.addEventListener('click',async e=>{
 if(e.target.closest('[data-attach]')){dialog.querySelector('input[name=file]').click();return;}
 if(e.target.closest('[data-remove-file]')){dialog.querySelector('input[name=file]').value='';dialog.querySelector('[data-selected-file]').hidden=true;updateComposer();return;}
 if(e.target.closest('[data-jump-bottom]')){scrollBottom();return;}
 const record=e.target.closest('[data-record]');if(record){const thread={...current};return openRecorder(record.dataset.record,async file=>{if(current.kind!==thread.kind||current.peer!==thread.peer)throw new ApiError('Диалог изменился. Откройте его заново.');const result=await uploadFile(file,'');await renderMessages([result.message],{own:true});});}
 const play=e.target.closest('[data-load-media]');if(play){play.disabled=true;try{const blob=await fetchAttachment(Number(play.dataset.loadMedia));if(!play.isConnected)return;const url=URL.createObjectURL(blob);objectUrls.add(url);const media=play.parentElement.querySelector('[data-media-id]');media.src=url;media.hidden=false;play.hidden=true;media.play().catch(()=>{});}catch(err){play.disabled=false;showError(err.message);}return;}
 if(e.target.closest('[data-start-call],[data-join-call],[data-resume-call]'))return handleCallClick(e,current.peer,current.title);
 const download=e.target.closest('[data-download-id]');if(download){download.disabled=true;try{await downloadAttachment(Number(download.dataset.downloadId),download.dataset.downloadName);}catch(err){showError(err.message);}finally{download.disabled=false;}return;}
 const peer=e.target.closest('[data-peer]');if(peer){const p=people.people.find(x=>x.id===Number(peer.dataset.peer));return openThread('peer',p.id,p.name);}
 const owner=e.target.closest('[data-owner-a]');if(owner)return ownerView(Number(owner.dataset.ownerA),Number(owner.dataset.ownerB),owner.dataset.ownerTitle);
 const accept=e.target.closest('[data-chat-accept]');if(accept){accept.disabled=true;try{await api('/chat/rules/accept',{method:'POST',body:{version:accept.dataset.version,sha256:accept.dataset.sha}});await home();}catch(err){accept.disabled=false;showError(err.message);}return;}
 const action=e.target.closest('[data-chat]')?.dataset.chat;
 if(action==='close')close();
 if(action==='home')home();
 if(action==='general')openThread('general',null,'Общий чат');
 if(action==='owner')ownerThreads();
 if(action==='rules')showRules();
});
dialog.addEventListener('submit',async e=>{
 if(e.target.id!=='pbChatCompose')return;e.preventDefault();
 const thread=current,key=draftKey(),form=e.target;if(form.dataset.sending)return;
 const field=form.elements.body;const file=e.target.elements.file.files?.[0]||null;const body=field.value.trim();
 if(!body&&!file)return;
 form.dataset.sending='true';const button=form.querySelector('[type=submit]');button.disabled=true;field.disabled=true;form.elements.file.disabled=true;
 for(const b of form.querySelectorAll('[data-record]'))b.disabled=true;
 const attach=e.target.querySelector('.pb-chat-attach');if(attach)attach.classList.add('disabled');
 try{
  let d;
  if(file)d=await uploadFile(file,body);
  else{const peerId=current.kind==='general'?null:current.peer;d=await api('/chat/messages',{method:'POST',body:{peerId,body}});}
  drafts.delete(key);field.value='';e.target.elements.file.value='';const selected=e.target.querySelector('[data-selected-file]');if(selected){selected.hidden=true;selected.textContent='';}
  if(current===thread){await renderMessages([d.message],{own:true});updateComposer();}
 }catch(err){if(current===thread)showError(err.message);}finally{delete form.dataset.sending;button.disabled=false;field.disabled=false;form.elements.file.disabled=false;for(const b of form.querySelectorAll('[data-record]'))b.disabled=false;if(attach)attach.classList.remove('disabled');if(field.isConnected&&matchMedia('(pointer:fine)').matches)field.focus();}
});
function inject(){
 const top=document.querySelector('#topbar');if(!top||top.querySelector('[data-open-chat]'))return;
 const btn=document.createElement('button');btn.className='pb-chat-trigger';btn.dataset.openChat='1';btn.innerHTML=`${icon('chat')}<span>Чат</span>`;btn.setAttribute('aria-label','Рабочий чат');
 btn.addEventListener('click',home);top.append(btn);
}
async function boot(){try{me=await api('/me');inject();startUnreadPoll();startPresence();initCalls(me);}catch{me=null;}}
const observer=new MutationObserver(()=>{if(me)inject();else if(document.querySelector('#topbar')?.children.length)boot();});
observer.observe(document.querySelector('#topbar'),{childList:true});boot();

document.addEventListener('visibilitychange',()=>{sendHeartbeat();if(document.visibilityState==='visible'){refreshUnread();refreshPresence();}});
window.addEventListener('pagehide',()=>sendHeartbeat(false));
window.addEventListener('pageshow',()=>{sendHeartbeat();refreshPresence();});
window.addEventListener('offline',()=>{onlinePeople=null;renderPresence();sendHeartbeat(false);});
window.addEventListener('online',()=>{sendHeartbeat();refreshPresence();});
document.addEventListener('pb-calls-updated',()=>{if(!dialog.open)return;const toolbar=dialog.querySelector('.pb-call-toolbar');if(toolbar)toolbar.outerHTML=callToolbar(current.peer,current.title);});
