import {localAction} from '/app/js/localstore.js';
import {api,ApiError} from '/app/js/api.js';
import {esc,ROLE_NAMES} from '/app/js/domain.js';
import {initCalls,resetCalls,callToolbar,handleCallClick} from '/work-chat/calls.js';
import {icon,avatar} from '/work-chat/ui.js';
import {openRecorder,closeRecorder} from '/work-chat/recorder.js';

const dialog=document.createElement('dialog');
dialog.className='pb-chat-dialog';
dialog.setAttribute('aria-labelledby','pbChatTitle');
document.body.append(dialog);
const css=document.createElement('link');css.rel='stylesheet';css.href='/work-chat/chat.css';document.head.append(css);

let me=null, people=null, current={kind:'closed'}, timer=null, unreadTimer=null, previousFocus=null;
let sessionGeneration=0,bootPromise=null,autoBoot=true,unreadRevision=0;
const drafts=new Map(),attachmentDrafts=new Map(),outbox=new Map();
let flushing=null;
const outboxPrefix=userId=>`pb-chat-outbox:${userId??me?.user?.id}:`;
const storedDraftKey=(key,userId=me?.user?.id)=>`pb-chat-draft:${userId}:${key}`;
const sameSession=(generation,userId)=>generation===sessionGeneration&&me?.user?.id===userId;
function saveDraft(key,value,userId=me?.user?.id){
 if(!userId)return;
 if(userId===me?.user?.id)drafts.set(key,value);
 try{if(value)localStorage.setItem(storedDraftKey(key,userId),value);else localStorage.removeItem(storedDraftKey(key,userId));}
 catch{if(userId===me?.user?.id)showError('Черновик хранится только до закрытия приложения: память устройства недоступна.');}
}
function readDraft(key){try{return drafts.get(key)??localStorage.getItem(storedDraftKey(key))??'';}catch{return drafts.get(key)||'';}}
function renderOutbox(){
 const host=dialog.querySelector('[data-chat-outbox]');if(!host)return;
 host.innerHTML=[...outbox.values()].filter(x=>x.threadKey===draftKey()).map(x=>`<article class="pb-chat-pending"><span>${esc(x.body||x.file?.name||'Вложение')}</span><small role="status">${esc(x.state==='sending'?'Отправляем…':x.error||(navigator.onLine?'Ожидает отправки':'Сохранено на устройстве · ждём сеть'))}</small>${x.state!=='sending'?`<button type="button" data-retry-send="${esc(x.clientId)}">Повторить</button>${x.state==='error'&&!x.retryable?`<button type="button" data-discard-send="${esc(x.clientId)}">Убрать из очереди</button>`:''}`:''}</article>`).join('');
}
async function queueMessage(thread,body,file){
 if(!me||!['general','peer'].includes(thread.kind))throw new ApiError('Откройте рабочий диалог заново.');
 if(file?.size>MAX_ATTACHMENT_BYTES)throw new ApiError('Файл больше 20 МБ. Выберите файл меньшего размера.',413);
 const generation=sessionGeneration,userId=me.user.id,clientId=crypto.randomUUID();
 const item={key:outboxPrefix(userId)+clientId,clientId,userId,threadKey:`${thread.kind}:${thread.peer||'general'}`,peerId:thread.kind==='general'?null:thread.peer,body,file,createdAt:Date.now(),state:'pending',retryable:true};
 try{await localAction('snapshots','readwrite',store=>store.put(item));}
 catch{throw new ApiError('Не удалось сохранить отправку на устройстве. Освободите память и повторите; текст и файл остаются в форме.');}
 if(!sameSession(generation,userId))return;
 outbox.set(clientId,item);renderOutbox();flushOutbox();
}
async function flushOutbox(){
 if(flushing||!me||!navigator.onLine)return;
 const job=flushing={generation:sessionGeneration,userId:me.user.id},blocked=new Set();
 try{for(const item of [...outbox.values()].sort((a,b)=>a.createdAt-b.createdAt)){
  if(!sameSession(job.generation,job.userId)||!navigator.onLine)break;
  if(blocked.has(item.threadKey))continue;
  if((item.state==='error'&&!item.retryable)||(item.nextAttemptAt||0)>Date.now()){blocked.add(item.threadKey);continue;}
  item.state='sending';renderOutbox();
  try{
   if(item.file)await uploadFile(item.file,item.body,item.peerId,item.clientId,job.userId);
   else await api('/chat/messages',{method:'POST',body:{peerId:item.peerId,body:item.body,clientId:item.clientId,expectedUserId:job.userId}});
   // Lost acknowledgements retry the same ID. Never send the next message early.
   await localAction('snapshots','readwrite',store=>store.delete(item.key));
   if(!sameSession(job.generation,job.userId))break;
   outbox.delete(item.clientId);
   if(draftKey()===item.threadKey)await poll();
  }catch(err){
   if(!sameSession(job.generation,job.userId))break;
   item.state='error';item.error=err.message;item.retryable=!err.status||err.status>=500||err.status===429;
   item.attempts=(item.attempts||0)+1;item.nextAttemptAt=Date.now()+Math.min(60000,1000*2**Math.min(item.attempts,6));
   try{await localAction('snapshots','readwrite',store=>store.put(item));}catch{}
   blocked.add(item.threadKey);
  }finally{if(sameSession(job.generation,job.userId))renderOutbox();}
 }}finally{if(flushing===job){flushing=null;if(sameSession(job.generation,job.userId)&&[...outbox.values()].some(item=>item.state==='pending'&&!blocked.has(item.threadKey)&&(item.nextAttemptAt||0)<=Date.now()))setTimeout(flushOutbox,0);}}
}
async function restoreOutbox(){
 const generation=sessionGeneration,userId=me?.user?.id;if(!userId)return;
 try{
  const prefix=outboxPrefix(userId),rows=await localAction('snapshots','readonly',store=>store.getAll(IDBKeyRange.bound(prefix,prefix+'\uffff')));if(!sameSession(generation,userId))return;
  for(const row of rows.sort((a,b)=>(a.createdAt||0)-(b.createdAt||0)))if(typeof row.key==='string'&&row.key.startsWith(outboxPrefix(userId))){
   row.state=row.state==='sending'?'pending':row.state;row.userId=userId;row.nextAttemptAt=0;outbox.set(row.clientId,row);
  }
  renderOutbox();flushOutbox();
 }catch{if(sameSession(generation,userId))showError('Не удалось прочитать сохранённые отправки. Проверьте память устройства.');}
}
const deletePanel=document.createElement('dialog');deletePanel.className='pb-record-dialog pb-chat-delete-dialog';deletePanel.setAttribute('aria-labelledby','pbDeleteTitle');document.body.append(deletePanel);
let pendingDelete=null,deleteFocus=null;
const objectUrls=new Set();
const MAX_ATTACHMENT_BYTES=20*1024*1024;
const presenceSession=crypto.randomUUID();
let presenceSequence=0,presenceTimer=null,presenceBusy=false,heartbeatBusy=false,onlinePeople=null,presenceAt=0;
let previewObserver=null,previewActive=0;const previewQueue=[];

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
 const generation=sessionGeneration,userId=me.user.id;presenceBusy=true;
 try{const data=await api('/chat/presence');if(sameSession(generation,userId)){onlinePeople=new Set(data.online);presenceAt=Date.now();}}
 catch{if(sameSession(generation,userId))onlinePeople=null;}
 finally{if(sameSession(generation,userId)){presenceBusy=false;renderPresence();}}
}
async function sendHeartbeat(online=document.visibilityState==='visible'&&navigator.onLine){
 if(!me||(online&&heartbeatBusy))return;
 const generation=sessionGeneration,userId=me.user.id,sequence=++presenceSequence;
 if(online)heartbeatBusy=true;
 try{await api('/chat/presence',{method:'POST',keepalive:!online,body:{sessionId:presenceSession,sequence,online}});}
 catch{/* Presence expires on the server if the device loses its connection. */}
 finally{if(online&&sameSession(generation,userId))heartbeatBusy=false;}
}
function startPresence(){
 if(presenceTimer)clearInterval(presenceTimer);
 sendHeartbeat();
 presenceTimer=setInterval(()=>{
  renderPresence();
  if(document.visibilityState==='visible'&&navigator.onLine){sendHeartbeat();refreshPresence();}
 },10000);
}

function clearObjectUrls(){previewObserver?.disconnect();previewObserver=null;previewQueue.length=0;for(const media of dialog.querySelectorAll('audio,video')){media.pause();media.removeAttribute('src');}for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.clear();}
function stopPoll(){if(timer){clearInterval(timer);timer=null;}}
function unreadBadge(n){return n>0?`<span class="pb-chat-unread">${n>99?'99+':n}</span>`:'';}
function setUnreadBadge(n){unreadRevision++;const btn=document.querySelector('[data-open-chat]');if(!btn)return;btn.innerHTML=`${icon('chat')}<span>Чат</span>${unreadBadge(Number(n)||0)}`;btn.setAttribute('aria-label',n>0?`Рабочий чат, непрочитанных: ${n}`:'Рабочий чат');}
async function refreshUnread(){if(!me)return;const generation=sessionGeneration,userId=me.user.id,revision=++unreadRevision;try{const data=await api('/chat/unread');if(sameSession(generation,userId)&&revision===unreadRevision){setUnreadBadge(data.total||0);applyUnread(data);}}catch(e){if(sameSession(generation,userId)&&revision===unreadRevision&&e.status===428)setUnreadBadge(0);}}
function applyUnread(data){if(!people)return;people.general.unread=data.general||0;for(const person of people.people)person.unread=data.people?.[person.id]||0;for(const row of dialog.querySelectorAll('[data-peer],[data-chat=general]')){const count=row.dataset.peer?data.people?.[row.dataset.peer]||0:data.general||0;row.querySelector('.pb-chat-unread')?.remove();row.insertAdjacentHTML('beforeend',unreadBadge(count));}}
function startUnreadPoll(){if(unreadTimer)clearInterval(unreadTimer);refreshUnread();unreadTimer=setInterval(()=>{if(document.visibilityState==='visible')refreshUnread();},10000);}
function close(){closeRecorder();stopPoll();current={kind:'closed'};clearObjectUrls();if(deletePanel.open)closeDelete();dialog.close();previousFocus?.focus?.();}
function shell(title,html){
 closeRecorder();clearObjectUrls();
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
 const key=draftKey(),file=form.elements.file.files?.[0];if(file)attachmentDrafts.set(key,file);else attachmentDrafts.delete(key);
 saveDraft(key,field.value);
}
function scrollBottom(){const box=dialog.querySelector('#pbChatMessages');if(box)box.scrollTop=box.scrollHeight;const jump=dialog.querySelector('[data-jump-bottom]');if(jump)jump.hidden=true;}
function showError(message){const el=dialog.querySelector('[data-chat-error]');if(el){el.textContent=message;el.hidden=false;}else shell('Рабочий чат',`<p class="pb-chat-error" role="alert">${esc(message)}</p><button class="pb-chat-btn" data-chat="home">Повторить</button>`);}
function roles(list){return (list||[]).map(r=>ROLE_NAMES[r]||r).join(' · ');}
function time(v){try{return new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}).format(new Date(v));}catch{return '';}}
function fileSize(bytes){if(!Number.isFinite(bytes))return '';if(bytes<1024)return `${bytes} Б`;if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} КБ`;return `${(bytes/1024/1024).toFixed(1)} МБ`;}

async function authFetch(path,{method='GET',body,timeout=60000}={}){
 return api(path,{method,body,timeoutMs:timeout,responseType:'response'});
}
async function fetchAttachment(id){const response=await authFetch(`/chat/attachments/${id}`);return response.blob();}
async function downloadAttachment(id,name){
 const generation=sessionGeneration,userId=me?.user?.id,blob=await fetchAttachment(id);if(!sameSession(generation,userId))return;const url=URL.createObjectURL(blob);
 const a=document.createElement('a');a.href=url;a.download=name||'file';a.rel='noopener';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
function queueImagePreview(img,id){previewQueue.push({img,id});drainPreviews();}
function drainPreviews(){while(previewActive<3&&previewQueue.length){const {img,id}=previewQueue.shift();if(!img.isConnected)continue;previewActive++;loadImagePreview(img,id).finally(()=>{previewActive--;drainPreviews();});}}
async function loadImagePreview(img,id){
 try{const blob=await fetchAttachment(id);if(!img.isConnected)return;const url=URL.createObjectURL(blob);objectUrls.add(url);const box=dialog.querySelector('#pbChatMessages'),near=box&&box.scrollHeight-box.scrollTop-box.clientHeight<100;img.onload=()=>{if(near&&img.isConnected)scrollBottom();};img.src=url;img.dataset.loaded='1';}
 catch{if(img.isConnected){img.alt='Фото временно недоступно';img.classList.add('failed');}}
}
async function ensureRules(view){
 const rules=await api('/chat/rules');
 if(current!==view)return false;
 if(rules.accepted)return true;
 shell('Правила Photo Boss',`<article class="pb-chat-rules">${esc(rules.text)}</article><p class="pb-chat-muted">Подтверждение относится ко всему рабочему пространству. Это не договор оказания услуг и не отдельное согласие на обработку персональных данных.</p><p class="pb-chat-error" data-chat-error hidden></p><button class="pb-chat-btn primary" data-chat-accept data-version="${esc(rules.version)}" data-sha="${esc(rules.sha256)}">Принять общие правила и открыть чат</button>`);
 return false;
}
async function home(){
 stopPoll();const view=current={kind:'home',peer:null,title:'Рабочий чат'};
 shell('Сообщения','<p class="pb-chat-muted" role="status">Загружаем чаты…</p>');
 // Only people loaded after accepting this account's rules may be reused offline.
 // resetWorkChat clears them before a different account can enter the workspace.
 if(!navigator.onLine&&people){shell('Сообщения',navigation());renderPresence();return;}
 try{
  if(!await ensureRules(view))return;
  const data=await api('/chat/people');if(current!==view)return;people=data;
  onlinePeople=new Set(people.people.filter(p=>p.online).map(p=>p.id));presenceAt=Date.now();
  setUnreadBadge(people.totalUnread||0);
  shell('Сообщения',navigation());
  renderPresence();sendHeartbeat();refreshPresence();
 }catch(e){if(current===view)showError(e.message);}
}
function attachmentMarkup(a){
 if(!a)return '';
 if(a.isAudio||a.isVideo){const tag=a.isAudio?'audio':'video';return `<div class="pb-chat-media"><button type="button" data-load-media="${a.id}"><span class="pb-chat-media-play">${icon(a.isAudio?'play':'video')}</span><span><strong>${a.isAudio?'Голосовое сообщение':'Видеосообщение'}</strong><small>${esc(fileSize(a.size))} · ${a.isAudio?'Прослушать':'Посмотреть'}</small></span></button><${tag} controls playsinline preload="none" data-media-id="${a.id}" hidden></${tag}></div>`;}
 if(a.isImage)return `<button type="button" class="pb-chat-photo" data-download-id="${a.id}" data-download-name="${esc(a.name)}" aria-label="Открыть фото"><img data-preview-id="${a.id}" alt="Фото: ${esc(a.name)}"></button><div class="pb-chat-file-caption">${esc(a.name)} · ${esc(fileSize(a.size))}</div>`;
 return `<button type="button" class="pb-chat-file" data-download-id="${a.id}" data-download-name="${esc(a.name)}"><span>${icon('file')}</span><span><strong>${esc(a.name)}</strong><small>${esc(fileSize(a.size))}</small></span></button>`;
}
function messageDay(value){const d=new Date(value);return Number.isNaN(d.getTime())?'':d.toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:d.getFullYear()!==new Date().getFullYear()?'numeric':undefined});}
function renderMessages(items,{readonly=false,own=false}={}){
 const box=dialog.querySelector('#pbChatMessages');if(!box)return;
 const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100, first=!box.querySelector('[data-message-id]');let added=0;
 for(const m of items){
  if(current.deletedIds?.has(m.id)||box.querySelector(`[data-message-id="${m.id}"]`))continue;
  box.querySelector('.pb-chat-empty')?.remove();
  const day=messageDay(m.createdAt),previous=box.querySelector('[data-message-id]:last-child');
  if(box.dataset.day!==day){const label=document.createElement('div');label.className='pb-chat-day';label.textContent=day;box.append(label);box.dataset.day=day;}
  const mine=m.senderId===me?.user?.id;
  const el=document.createElement('article');el.className='pb-chat-message '+(mine?'mine':'');el.dataset.messageId=m.id;el.dataset.sender=m.senderId;el.dataset.created=m.createdAt;
  if(previous?.dataset.sender===String(m.senderId)&&messageDay(previous.dataset.created)===day&&new Date(m.createdAt)-new Date(previous.dataset.created)<300000)el.classList.add('is-grouped');
  const clock=new Date(m.createdAt).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
  el.innerHTML=`${!mine&&(current.kind==='general'||readonly)?`<div class="pb-chat-sender">${esc(m.senderName)}</div>`:''}${attachmentMarkup(m.attachment)}${m.body?`<div class="pb-chat-bubble">${esc(m.body).replace(/\n/g,'<br>')}</div>`:''}<div class="pb-chat-meta">${mine&&!readonly&&me?.user?.roles?.includes('OWNER')?`<details class="pb-chat-message-actions"><summary aria-label="Действия с сообщением">${icon('more')}</summary><button type="button" data-delete-message="${m.id}">${icon('trash')} Удалить у всех</button></details>`:''}<time datetime="${esc(m.createdAt)}">${esc(clock)}</time>${mine?`<span aria-label="Отправлено" title="Отправлено">${icon('check')}</span>`:''}</div>`;
  box.append(el);added++;
  for(const img of el.querySelectorAll('[data-preview-id]')){
   if('IntersectionObserver' in window){previewObserver??=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){previewObserver?.unobserve(entry.target);queueImagePreview(entry.target,Number(entry.target.dataset.previewId));}},{root:box,rootMargin:'100px'});previewObserver.observe(img);}
   else queueImagePreview(img,Number(img.dataset.previewId));
  }
 }
 if(!box.querySelector('[data-message-id]')&&!box.querySelector('.pb-chat-empty'))box.innerHTML=`<div class="pb-chat-empty">${icon('chat')}<strong>Здесь начинается разговор</strong><span>Напишите сообщение, отправьте фото<br>или позвоните.</span></div>`;
 if(added){if(first||nearBottom||own)scrollBottom();else{const jump=dialog.querySelector('[data-jump-bottom]');if(jump)jump.hidden=false;}}
 if(readonly)dialog.querySelector('.pb-chat-compose')?.remove();
}
function applyDeletions(ids){
 const box=dialog.querySelector('#pbChatMessages');if(!box||!ids.length)return;
 current.deletedIds??=new Set();
 for(const id of ids){
  current.deletedIds.add(id);const message=box.querySelector(`[data-message-id="${id}"]`);if(!message)continue;
  for(const media of message.querySelectorAll('audio,video,img')){media.pause?.();const url=media.getAttribute('src');if(objectUrls.delete(url))URL.revokeObjectURL(url);media.removeAttribute('src');}
  message.remove();
 }
 for(const day of box.querySelectorAll('.pb-chat-day'))if(!day.nextElementSibling?.matches('[data-message-id]'))day.remove();
 let previous=null;for(const message of box.querySelectorAll('[data-message-id]')){message.classList.toggle('is-grouped',Boolean(previous&&previous.dataset.sender===message.dataset.sender&&messageDay(previous.dataset.created)===messageDay(message.dataset.created)&&new Date(message.dataset.created)-new Date(previous.dataset.created)<300000));previous=message;}
 if(previous)box.dataset.day=messageDay(previous.dataset.created);else delete box.dataset.day;
 renderMessages([]);
}
function askDelete(id){
 if(!me?.user?.roles?.includes('OWNER'))return;
 const message=dialog.querySelector(`[data-message-id="${id}"].mine`);if(!message)return;
 message.querySelector('details')?.removeAttribute('open');deleteFocus=document.activeElement;pendingDelete={id,thread:current,generation:sessionGeneration,userId:me.user.id};
 deletePanel.innerHTML=`<div class="pb-record-hero">${icon('trash')}</div><h2 id="pbDeleteTitle">Удалить сообщение?</h2><p class="pb-record-note">Сообщение и вложение исчезнут у всех участников этого чата. Отменить удаление нельзя.</p><p data-delete-error role="alert" hidden></p><div class="pb-record-actions"><button data-delete-cancel autofocus>Отмена</button><button data-delete-confirm class="danger">Удалить у всех</button></div>`;
 deletePanel.showModal();
}
function closeDelete(){deletePanel.close();pendingDelete=null;deleteFocus?.focus?.();}
deletePanel.addEventListener('cancel',e=>{e.preventDefault();if(!deletePanel.querySelector('[data-delete-confirm]')?.disabled)closeDelete();});
deletePanel.addEventListener('click',async e=>{
 if(e.target.closest('[data-delete-cancel]'))return closeDelete();
 const button=e.target.closest('[data-delete-confirm]');if(!button||!pendingDelete||button.disabled)return;
 const deleting=pendingDelete;button.disabled=true;deletePanel.querySelector('[data-delete-cancel]').disabled=true;
 try{await api(`/chat/messages/${deleting.id}`,{method:'DELETE'});if(!sameSession(deleting.generation,deleting.userId))return;if(current===deleting.thread)applyDeletions([deleting.id]);closeDelete();refreshUnread();}
 catch(err){if(!sameSession(deleting.generation,deleting.userId))return;const error=deletePanel.querySelector('[data-delete-error]');error.hidden=false;error.textContent=err.message;button.disabled=false;deletePanel.querySelector('[data-delete-cancel]').disabled=false;}
});

async function poll(){
 const thread=current,generation=sessionGeneration,userId=me?.user?.id;
 if(thread.loading){thread.needsPoll=true;return;}
 if(!userId||!dialog.open||document.visibilityState!=='visible'||!['general','peer','owner-view'].includes(thread.kind))return;
 thread.loading=true;let again=false;
 try{
  for(let page=0;page<8;page++){
   const cursor=thread.last||0;
   const path=thread.kind==='owner-view'?`/chat/owner/messages?a=${thread.a}&b=${thread.b}&after=${cursor}`:`/chat/messages?peer=${thread.kind==='general'?'general':thread.peer}&after=${cursor}&markRead=0`;
   const data=await api(path+(thread.deletionCursor===undefined?'':`&deletedAfter=${thread.deletionCursor}`));
   if(!sameSession(generation,userId)||current!==thread||!dialog.open)return;
   if(Number.isInteger(data.deletionCursor))thread.deletionCursor=data.deletionCursor;
   applyDeletions(data.deletedIds||[]);
   renderMessages(data.messages,{readonly:thread.kind==='owner-view'});
   // The GET cursor must never advance from a send response or another thread.
   for(const message of data.messages)thread.last=Math.max(thread.last||0,message.id);
   const error=dialog.querySelector('[data-chat-error]');if(error)error.hidden=true;
   if(data.unread){setUnreadBadge(data.unread.total||0);applyUnread(data.unread);}
   const more=data.hasMore??data.messages.length===100;
   again=Boolean((more&&(thread.last||0)>cursor)||data.deletedHasMore);
   if(!again)break;
   if(document.visibilityState!=='visible')return;
  }
  if(!again&&thread.kind!=='owner-view'&&(thread.last||0)>(thread.lastAcknowledged||0)&&sameSession(generation,userId)&&current===thread&&dialog.open&&document.visibilityState==='visible'){
   const read=await api('/chat/read',{method:'POST',body:{peerId:thread.kind==='general'?null:thread.peer,lastReadMessageId:thread.last,expectedUserId:userId}});
   if(!sameSession(generation,userId)||current!==thread||!dialog.open)return;
   thread.lastAcknowledged=thread.last;
   if(read.unread){setUnreadBadge(read.unread.total||0);applyUnread(read.unread);}else refreshUnread();
  }
 }catch(e){again=false;if(sameSession(generation,userId)&&current===thread)showError(e.message);}
 finally{
  thread.loading=false;
  const retry=again||thread.needsPoll;thread.needsPoll=false;
  if(retry&&sameSession(generation,userId)&&current===thread&&dialog.open&&document.visibilityState==='visible')setTimeout(poll,0);
 }
}
async function openThread(kind,peer=null,title='Общий чат'){
 current={kind,peer,title,last:0};stopPoll();
 shell(title,`<div class="pb-chat-history"><div id="pbChatMessages" class="pb-chat-messages" aria-live="polite" aria-label="Сообщения"></div><button class="pb-chat-jump" data-jump-bottom aria-label="К новым сообщениям" hidden>${icon('down')}</button></div><div data-chat-outbox class="pb-chat-outbox" aria-label="Отправка сообщений"></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p><form class="pb-chat-compose" id="pbChatCompose"><div class="pb-chat-selected" data-selected-file hidden></div><div class="pb-chat-input-row"><button type="button" class="pb-chat-attach" data-attach aria-label="Прикрепить фото или файл" title="Фото или файл до 20 МБ">${icon('attach')}</button><input type="file" name="file" hidden><textarea name="body" maxlength="2000" rows="1" placeholder="Сообщение" aria-label="Сообщение"></textarea><button type="button" class="pb-chat-record" data-record="video" aria-label="Записать видеосообщение" title="Видеосообщение">${icon('video')}</button><button type="button" class="pb-chat-record" data-record="audio" aria-label="Записать голосовое сообщение" title="Голосовое сообщение">${icon('mic')}</button><button class="pb-chat-send" type="submit" aria-label="Отправить" title="Отправить" hidden>${icon('send')}</button></div></form>`);
 dialog.querySelector('.pb-chat-close').insertAdjacentHTML('beforebegin',callToolbar(peer,title));
 const heading=dialog.querySelector('.pb-chat-heading');
 heading.insertAdjacentHTML('beforeend',kind==='peer'?presenceMarkup(peer):'<span class="pb-chat-subtitle">Вся команда · звонки до 6 участников</span>');
 heading.querySelector('.pb-chat-presence')?.setAttribute('aria-live','polite');
 dialog.querySelector('textarea').value=readDraft(draftKey());const savedFile=attachmentDrafts.get(draftKey());if(savedFile){const transfer=new DataTransfer();transfer.items.add(savedFile);dialog.querySelector('input[name=file]').files=transfer.files;renderSelectedFile(savedFile);}updateComposer();renderOutbox();
 dialog.querySelector('#pbChatMessages').addEventListener('scroll',()=>{const box=dialog.querySelector('#pbChatMessages');if(box.scrollHeight-box.scrollTop-box.clientHeight<100)dialog.querySelector('[data-jump-bottom]').hidden=true;},{passive:true});
 renderPresence();refreshPresence();
 const thread=current;await poll();if(current===thread&&dialog.open)timer=setInterval(poll,4000);
}
async function ownerThreads(){
 stopPoll();const view=current={kind:'owner',peer:null};
 shell('Контроль диалогов','<p class="pb-chat-muted" role="status">Загружаем диалоги…</p>');
 try{
  const d=await api('/chat/owner/threads');if(current!==view)return;
  const rows=d.threads.map(t=>`<button class="pb-chat-thread" data-owner-a="${t.a.id}" data-owner-b="${t.b.id}" data-owner-title="${esc(t.a.name+' ↔ '+t.b.name)}"><strong>${esc(t.a.name)} ↔ ${esc(t.b.name)}</strong><small>${esc(t.last)} · ${esc(time(t.createdAt))}</small></button>`).join('');
  shell('Контроль диалогов',`<p class="pb-chat-muted">Только рабочие диалоги внутри Photo Boss. Обычный Telegram не подключён.</p><div class="pb-chat-list">${rows||'<p class="pb-chat-muted">Личных диалогов сотрудников пока нет.</p>'}</div>`);
 }catch(e){if(current===view)showError(e.message);}
}
async function ownerView(a,b,title){
 current={kind:'owner-view',a,b,title,last:0};stopPoll();
 shell(title,`<div class="pb-chat-readonly">Просмотр рабочего диалога</div><div id="pbChatMessages" class="pb-chat-messages" aria-live="polite"></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p>`);
 const view=current;await poll();if(current===view&&dialog.open)timer=setInterval(poll,4000);
}
async function showRules(){
 stopPoll();const view=current={kind:'rules',peer:null};
 shell('Общие правила','<p class="pb-chat-muted" role="status">Загружаем правила…</p>');
 try{const r=await api('/chat/rules');if(current!==view)return;shell('Общие правила',`<article class="pb-chat-rules">${esc(r.text)}</article><p class="pb-chat-muted">Редакция: ${esc(r.version)} · хранение сообщений до ${r.retentionDays} дней.</p>`);}catch(e){if(current===view)showError(e.message);}
}
async function uploadFile(file,caption,peerId,clientId,userId){
 if(file.size>MAX_ATTACHMENT_BYTES)throw new ApiError('Файл больше 20 МБ.',413);
 const peer=peerId===null?'general':String(peerId);
 const form=new FormData();form.append('peerId',peer);form.append('caption',caption);form.append('clientId',clientId);form.append('expectedUserId',String(userId));form.append('file',file,file.name);
 const response=await authFetch('/chat/attachments',{method:'POST',body:form,timeout:90000});
 return response.json();
}

dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
function renderSelectedFile(file){
 const selected=dialog.querySelector('[data-selected-file]');if(!selected)return;
 selected.hidden=!file;selected.innerHTML=file?`${icon('file')}<span>${esc(file.name)}<small>${fileSize(file.size)}</small></span><button type="button" data-remove-file aria-label="Убрать файл">${icon('close')}</button>`:'';
}
dialog.addEventListener('change',e=>{
 if(e.target.name!=='file')return;
 const file=e.target.files?.[0];renderSelectedFile(file);updateComposer();
 if(file?.size>MAX_ATTACHMENT_BYTES)showError('Файл больше 20 МБ. Выберите файл меньшего размера.');
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
 const retry=e.target.closest('[data-retry-send]');if(retry){const item=outbox.get(retry.dataset.retrySend);if(item&&item.state!=='sending'){item.retryable=true;item.state='pending';item.nextAttemptAt=0;renderOutbox();flushOutbox();}return;}
 const discard=e.target.closest('[data-discard-send]');if(discard){const item=outbox.get(discard.dataset.discardSend),generation=sessionGeneration,userId=me?.user?.id;if(item?.state==='error'&&!item.retryable){discard.disabled=true;try{await localAction('snapshots','readwrite',store=>store.delete(item.key));if(sameSession(generation,userId)){outbox.delete(item.clientId);renderOutbox();flushOutbox();}}catch(err){if(sameSession(generation,userId))showError(err.message);}finally{discard.disabled=false;}}return;}
 const remove=e.target.closest('[data-delete-message]');if(remove)return askDelete(Number(remove.dataset.deleteMessage));
 if(e.target.closest('[data-attach]')){dialog.querySelector('input[name=file]').click();return;}
 if(e.target.closest('[data-remove-file]')){dialog.querySelector('input[name=file]').value='';dialog.querySelector('[data-selected-file]').hidden=true;updateComposer();return;}
 if(e.target.closest('[data-jump-bottom]')){scrollBottom();return;}
 const record=e.target.closest('[data-record]');if(record){const thread=current,generation=sessionGeneration,userId=me?.user?.id;return openRecorder(record.dataset.record,async file=>{if(!sameSession(generation,userId)||current!==thread)throw new ApiError('Диалог изменился. Откройте его заново.');await queueMessage(thread,'',file);});}
 const play=e.target.closest('[data-load-media]');if(play){const view=current,generation=sessionGeneration,userId=me?.user?.id;play.disabled=true;try{const blob=await fetchAttachment(Number(play.dataset.loadMedia));if(!sameSession(generation,userId)||current!==view||!play.isConnected)return;const url=URL.createObjectURL(blob);objectUrls.add(url);const media=play.parentElement.querySelector('[data-media-id]');media.src=url;media.hidden=false;play.hidden=true;media.play().catch(()=>{});}catch(err){if(sameSession(generation,userId)&&current===view&&play.isConnected){play.disabled=false;showError(err.message);}}return;}
 if(e.target.closest('[data-start-call],[data-join-call],[data-resume-call]'))return handleCallClick(e,current.peer,current.title);
 const download=e.target.closest('[data-download-id]');if(download){const view=current,generation=sessionGeneration,userId=me?.user?.id;download.disabled=true;try{await downloadAttachment(Number(download.dataset.downloadId),download.dataset.downloadName);}catch(err){if(sameSession(generation,userId)&&current===view&&download.isConnected)showError(err.message);}finally{download.disabled=false;}return;}
 const peer=e.target.closest('[data-peer]');if(peer){const p=people.people.find(x=>x.id===Number(peer.dataset.peer));return openThread('peer',p.id,p.name);}
 const owner=e.target.closest('[data-owner-a]');if(owner)return ownerView(Number(owner.dataset.ownerA),Number(owner.dataset.ownerB),owner.dataset.ownerTitle);
 const accept=e.target.closest('[data-chat-accept]');if(accept){const view=current,generation=sessionGeneration,userId=me?.user?.id;accept.disabled=true;try{await api('/chat/rules/accept',{method:'POST',body:{version:accept.dataset.version,sha256:accept.dataset.sha}});if(sameSession(generation,userId)&&current===view&&dialog.open)await home();}catch(err){if(sameSession(generation,userId)&&current===view&&accept.isConnected){accept.disabled=false;showError(err.message);}}return;}
 const action=e.target.closest('[data-chat]')?.dataset.chat;
 if(action==='close')close();
 if(action==='home')home();
 if(action==='general')openThread('general',null,'Общий чат');
 if(action==='owner')ownerThreads();
 if(action==='rules')showRules();
});
dialog.addEventListener('submit',async e=>{
 if(e.target.id!=='pbChatCompose')return;e.preventDefault();
 const thread=current,generation=sessionGeneration,userId=me?.user?.id,key=draftKey(),form=e.target;if(form.dataset.sending)return;
 const field=form.elements.body;const file=e.target.elements.file.files?.[0]||null;const body=field.value.trim();
 if(!body&&!file)return;
 form.dataset.sending='true';const button=form.querySelector('[type=submit]');button.disabled=true;field.disabled=true;form.elements.file.disabled=true;
 for(const b of form.querySelectorAll('[data-record]'))b.disabled=true;
 const attach=e.target.querySelector('.pb-chat-attach');if(attach)attach.classList.add('disabled');
 try{
  await queueMessage(thread,body,file);
  if(!sameSession(generation,userId))return;
  if(readDraft(key)===field.value)saveDraft(key,'');attachmentDrafts.delete(key);field.value='';e.target.elements.file.value='';const selected=e.target.querySelector('[data-selected-file]');if(selected){selected.hidden=true;selected.textContent='';}
  if(current===thread){dialog.querySelector('[data-chat-error]').hidden=true;updateComposer();}
 }catch(err){if(sameSession(generation,userId)&&current===thread)showError(err.message);}finally{delete form.dataset.sending;button.disabled=false;field.disabled=false;form.elements.file.disabled=false;for(const b of form.querySelectorAll('[data-record]'))b.disabled=false;if(attach)attach.classList.remove('disabled');if(field.isConnected&&matchMedia('(pointer:fine)').matches)field.focus();}
});
function inject(){
 const top=document.querySelector('#topbar');if(!top||top.querySelector('[data-open-chat]'))return;
 const btn=document.createElement('button');btn.className='pb-chat-trigger';btn.dataset.openChat='1';btn.innerHTML=`${icon('chat')}<span>Чат</span>`;btn.setAttribute('aria-label','Рабочий чат');
 btn.addEventListener('click',home);top.append(btn);
}
export function resetWorkChat(){
 autoBoot=false;sessionGeneration++;bootPromise=null;me=null;people=null;current={kind:'closed'};
 closeRecorder();resetCalls();stopPoll();if(unreadTimer)clearInterval(unreadTimer);unreadTimer=null;
 if(presenceTimer)clearInterval(presenceTimer);presenceTimer=null;
 flushing=null;presenceBusy=false;heartbeatBusy=false;onlinePeople=null;presenceAt=0;
 drafts.clear();attachmentDrafts.clear();outbox.clear();clearObjectUrls();
 if(deletePanel.open)closeDelete();pendingDelete=null;previousFocus=null;
 if(dialog.open)dialog.close();dialog.innerHTML='';document.querySelector('[data-open-chat]')?.remove();
}
export async function initWorkChat(userData=null){
 if(userData?.user?.id){
  if(me?.user?.id===userData.user.id){me=userData;inject();initCalls(me);return;}
  resetWorkChat();
  autoBoot=true;me=userData;inject();startUnreadPoll();startPresence();initCalls(me);await restoreOutbox();return;
 }
 autoBoot=true;if(bootPromise)return bootPromise;
 const generation=sessionGeneration;
 const promise=(async()=>{try{
  const data=await api('/me');if(generation!==sessionGeneration||!autoBoot)return;
  me=data;inject();startUnreadPoll();startPresence();initCalls(me);await restoreOutbox();
 }catch{if(generation===sessionGeneration)me=null;}})();
 bootPromise=promise;try{await promise;}finally{if(bootPromise===promise)bootPromise=null;}
}
document.addEventListener('pb-chat-account-ready',event=>{initWorkChat(event.detail);});
document.addEventListener('pb-chat-account-reset',()=>{resetWorkChat();});
const observer=new MutationObserver(()=>{if(me)inject();else if(autoBoot&&document.querySelector('#topbar')?.children.length)initWorkChat();});
const topbar=document.querySelector('#topbar');if(topbar)observer.observe(topbar,{childList:true});initWorkChat();

document.addEventListener('visibilitychange',()=>{sendHeartbeat();if(document.visibilityState==='visible'){refreshUnread();refreshPresence();poll();flushOutbox();}});
window.addEventListener('pagehide',()=>sendHeartbeat(false));
window.addEventListener('pageshow',()=>{sendHeartbeat();refreshPresence();});
window.addEventListener('offline',()=>{onlinePeople=null;renderPresence();renderOutbox();sendHeartbeat(false);});
window.addEventListener('online',()=>{sendHeartbeat();refreshPresence();poll();flushOutbox();});
setInterval(()=>{if(document.visibilityState==='visible')flushOutbox();},10000);
document.addEventListener('pb-calls-updated',()=>{if(!dialog.open)return;const toolbar=dialog.querySelector('.pb-call-toolbar');if(toolbar)toolbar.outerHTML=callToolbar(current.peer,current.title);});

// Keep the composer above the software keyboard in mobile WebViews.
function resizeChatViewport(){const v=window.visualViewport;if(!v||v.scale!==1)return;document.documentElement.style.setProperty('--pb-viewport-height',`${v.height}px`);document.documentElement.style.setProperty('--pb-viewport-top',`${v.offsetTop}px`);}
window.visualViewport?.addEventListener('resize',resizeChatViewport);
window.visualViewport?.addEventListener('scroll',resizeChatViewport);
resizeChatViewport();

