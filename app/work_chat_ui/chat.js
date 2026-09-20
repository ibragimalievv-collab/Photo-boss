import {api,ApiError} from '/app/js/api.js';
import {esc,ROLE_NAMES} from '/app/js/domain.js';

const dialog=document.createElement('dialog');
dialog.className='pb-chat-dialog';
dialog.setAttribute('aria-labelledby','pbChatTitle');
document.body.append(dialog);
const css=document.createElement('link');css.rel='stylesheet';css.href='/work-chat/chat.css';document.head.append(css);

let me=null, people=null, current={kind:'general',peer:null,title:'Общий чат'}, timer=null, unreadTimer=null, last=0, busy=false, previousFocus=null;
const objectUrls=new Set();
const MAX_ATTACHMENT_BYTES=20*1024*1024;

function clearObjectUrls(){for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.clear();}
function stopPoll(){if(timer){clearInterval(timer);timer=null;}}
function unreadBadge(n){return n>0?`<span class="pb-chat-unread">${n>99?'99+':n}</span>`:'';}
function setUnreadBadge(n){const btn=document.querySelector('[data-open-chat]');if(!btn)return;btn.innerHTML=`<span>Чат</span>${unreadBadge(Number(n)||0)}`;btn.setAttribute('aria-label',n>0?`Рабочий чат, непрочитанных: ${n}`:'Рабочий чат');}
async function refreshUnread(){if(!me)return;try{const data=await api('/chat/unread');setUnreadBadge(data.total||0);}catch(e){if(e.status===428)setUnreadBadge(0);}}
function startUnreadPoll(){if(unreadTimer)clearInterval(unreadTimer);refreshUnread();unreadTimer=setInterval(()=>{if(document.visibilityState==='visible')refreshUnread();},10000);}
function close(){stopPoll();clearObjectUrls();dialog.close();previousFocus?.focus?.();}
function shell(title,html){clearObjectUrls();dialog.innerHTML=`<header class="pb-chat-head"><button class="pb-chat-back" data-chat="home" aria-label="Назад">‹</button><h2 id="pbChatTitle">${esc(title)}</h2><button class="pb-chat-close" data-chat="close" aria-label="Закрыть">×</button></header><div class="pb-chat-body">${html}</div>`;if(!dialog.open){previousFocus=document.activeElement;dialog.showModal();}}
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
 try{const blob=await fetchAttachment(id);const url=URL.createObjectURL(blob);objectUrls.add(url);img.src=url;img.dataset.loaded='1';}
 catch{img.alt='Фото временно недоступно';img.classList.add('failed');}
}
async function ensureRules(){
 const rules=await api('/chat/rules');
 if(rules.accepted)return true;
 shell('Правила Photo Boss',`<article class="pb-chat-rules">${esc(rules.text)}</article><p class="pb-chat-muted">Подтверждение относится ко всему рабочему пространству. Это не договор оказания услуг и не отдельное согласие на обработку персональных данных.</p><p class="pb-chat-error" data-chat-error hidden></p><button class="pb-chat-btn primary" data-chat-accept data-version="${esc(rules.version)}" data-sha="${esc(rules.sha256)}">Принять общие правила и открыть чат</button>`);
 return false;
}
async function home(){
 stopPoll();last=0;
 try{
  if(!await ensureRules())return;
  people=await api('/chat/people');
  setUnreadBadge(people.totalUnread||0);
  const rows=people.people.map(p=>`<button class="pb-chat-person" data-peer="${p.id}"><span class="pb-chat-avatar">${esc(p.name.slice(0,2).toUpperCase())}</span><span class="pb-chat-person-copy"><strong>${esc(p.name)}</strong><small>${esc(roles(p.roles))}</small></span>${unreadBadge(p.unread||0)}</button>`).join('');
  shell('Рабочий чат',`<button class="pb-chat-person featured" data-chat="general"><span class="pb-chat-avatar">👥</span><span class="pb-chat-person-copy"><strong>Общий чат</strong><small>Для всей команды</small></span>${unreadBadge(people.general?.unread||0)}</button><div class="pb-chat-section-title">Личные рабочие диалоги</div><div class="pb-chat-list">${rows||'<p class="pb-chat-muted">Других активных сотрудников пока нет.</p>'}</div>${people.ownerControl?'<button class="pb-chat-btn owner" data-chat="owner">Контроль диалогов сотрудников</button>':''}<button class="pb-chat-link" data-chat="rules">Общие правила</button>`);
 }catch(e){showError(e.message);}
}
function attachmentMarkup(a){
 if(!a)return '';
 if(a.isImage)return `<button type="button" class="pb-chat-photo" data-download-id="${a.id}" data-download-name="${esc(a.name)}" aria-label="Открыть фото"><img data-preview-id="${a.id}" alt="Фото: ${esc(a.name)}"></button><div class="pb-chat-file-caption">${esc(a.name)} · ${esc(fileSize(a.size))}</div>`;
 return `<button type="button" class="pb-chat-file" data-download-id="${a.id}" data-download-name="${esc(a.name)}"><span>📎</span><span><strong>${esc(a.name)}</strong><small>${esc(fileSize(a.size))}</small></span></button>`;
}
async function renderMessages(items,{readonly=false}={}){
 const box=dialog.querySelector('#pbChatMessages'); if(!box)return;
 for(const m of items){
  if(box.querySelector(`[data-message-id="${m.id}"]`))continue;
  const mine=m.senderId===me?.user?.id;
  const el=document.createElement('article');el.className='pb-chat-message '+(mine?'mine':'');el.dataset.messageId=m.id;
  const body=m.body?`<div class="pb-chat-bubble">${esc(m.body).replace(/\n/g,'<br>')}</div>`:'';
  el.innerHTML=`<div class="pb-chat-meta">${esc(m.senderName)} · ${esc(time(m.createdAt))}</div>${attachmentMarkup(m.attachment)}${body}`;
  box.append(el);last=Math.max(last,m.id);
  for(const img of el.querySelectorAll('[data-preview-id]'))loadImagePreview(img,Number(img.dataset.previewId));
 }
 if(items.length)box.scrollTop=box.scrollHeight;
 if(readonly)dialog.querySelector('.pb-chat-compose')?.remove();
}
async function poll(){
 if(busy||!dialog.open||!['general','peer','owner-view'].includes(current.kind))return;
 busy=true;
 try{
  if(current.kind==='owner-view'){
   const data=await api(`/chat/owner/messages?a=${current.a}&b=${current.b}&after=${last}`);
   await renderMessages(data.messages,{readonly:true});
  }else{
   const peer=current.kind==='general'?'general':current.peer;
   const data=await api(`/chat/messages?peer=${peer}&after=${last}`);
   await renderMessages(data.messages);
   if(data.unread)setUnreadBadge(data.unread.total||0);
  }
 }catch(e){showError(e.message);}finally{busy=false;}
}
async function openThread(kind,peer=null,title='Общий чат'){
 current={kind,peer,title};last=0;stopPoll();
 shell(title,`<div id="pbChatMessages" class="pb-chat-messages" aria-live="polite"></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p><form class="pb-chat-compose" id="pbChatCompose"><div class="pb-chat-input-row"><label class="pb-chat-attach" title="Фото или файл"><input type="file" name="file" hidden><span aria-hidden="true">📎</span><span class="sr-only">Прикрепить файл</span></label><textarea name="body" maxlength="2000" rows="2" placeholder="Рабочее сообщение…"></textarea><button class="pb-chat-send" type="submit">Отправить</button></div><div class="pb-chat-selected" data-selected-file hidden></div><div class="pb-chat-upload-note">Фото и файлы до 20 МБ. Опасные исполняемые файлы блокируются.</div></form>`);
 await poll();timer=setInterval(poll,4000);
}
async function ownerThreads(){
 stopPoll();last=0;
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
 if(!file){selected.hidden=true;selected.textContent='';return;}
 selected.hidden=false;selected.textContent=`📎 ${file.name} · ${fileSize(file.size)}`;
});
dialog.addEventListener('click',async e=>{
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
 const field=e.target.elements.body;const file=e.target.elements.file.files?.[0]||null;const body=field.value.trim();
 if(!body&&!file)return;
 const button=e.target.querySelector('[type=submit]');button.disabled=true;
 const attach=e.target.querySelector('.pb-chat-attach');if(attach)attach.classList.add('disabled');
 try{
  let d;
  if(file)d=await uploadFile(file,body);
  else{const peerId=current.kind==='general'?null:current.peer;d=await api('/chat/messages',{method:'POST',body:{peerId,body}});}
  field.value='';e.target.elements.file.value='';const selected=e.target.querySelector('[data-selected-file]');if(selected){selected.hidden=true;selected.textContent='';}
  await renderMessages([d.message]);
 }catch(err){showError(err.message);}finally{button.disabled=false;if(attach)attach.classList.remove('disabled');field.focus();}
});
function inject(){
 const top=document.querySelector('#topbar');if(!top||top.querySelector('[data-open-chat]'))return;
 const btn=document.createElement('button');btn.className='pb-chat-trigger';btn.dataset.openChat='1';btn.innerHTML='<span>Чат</span>';btn.setAttribute('aria-label','Рабочий чат');
 btn.addEventListener('click',home);top.append(btn);
}
async function boot(){try{me=await api('/me');inject();startUnreadPoll();}catch{me=null;}}
const observer=new MutationObserver(()=>{if(me)inject();else if(document.querySelector('#topbar')?.children.length)boot();});
observer.observe(document.querySelector('#topbar'),{childList:true});boot();

document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshUnread();});
