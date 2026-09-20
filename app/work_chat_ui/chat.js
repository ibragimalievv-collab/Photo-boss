import {api} from '/app/js/api.js';
import {esc,ROLE_NAMES} from '/app/js/domain.js';

const dialog=document.createElement('dialog');
dialog.className='pb-chat-dialog';
dialog.setAttribute('aria-labelledby','pbChatTitle');
document.body.append(dialog);
const css=document.createElement('link');css.rel='stylesheet';css.href='/work-chat/chat.css';document.head.append(css);

let me=null, people=null, current={kind:'general',peer:null,title:'Общий чат'}, timer=null, last=0, busy=false, previousFocus=null;

function stopPoll(){if(timer){clearInterval(timer);timer=null;}}
function close(){stopPoll();dialog.close();previousFocus?.focus?.();}
function shell(title,html){dialog.innerHTML=`<header class="pb-chat-head"><button class="pb-chat-back" data-chat="home" aria-label="Назад">‹</button><h2 id="pbChatTitle">${esc(title)}</h2><button class="pb-chat-close" data-chat="close" aria-label="Закрыть">×</button></header><div class="pb-chat-body">${html}</div>`;if(!dialog.open){previousFocus=document.activeElement;dialog.showModal();}}
function showError(message){const el=dialog.querySelector('[data-chat-error]');if(el){el.textContent=message;el.hidden=false;}else shell('Рабочий чат',`<p class="pb-chat-error" role="alert">${esc(message)}</p><button class="pb-chat-btn" data-chat="home">Повторить</button>`);}
function roles(list){return (list||[]).map(r=>ROLE_NAMES[r]||r).join(' · ');}
function time(v){try{return new Intl.DateTimeFormat('ru-RU',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}).format(new Date(v));}catch{return '';}}
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
  const rows=people.people.map(p=>`<button class="pb-chat-person" data-peer="${p.id}"><span class="pb-chat-avatar">${esc(p.name.slice(0,2).toUpperCase())}</span><span><strong>${esc(p.name)}</strong><small>${esc(roles(p.roles))}</small></span></button>`).join('');
  shell('Рабочий чат',`<button class="pb-chat-person featured" data-chat="general"><span class="pb-chat-avatar">👥</span><span><strong>Общий чат</strong><small>Для всей команды</small></span></button><div class="pb-chat-section-title">Личные рабочие диалоги</div><div class="pb-chat-list">${rows||'<p class="pb-chat-muted">Других активных сотрудников пока нет.</p>'}</div>${people.ownerControl?'<button class="pb-chat-btn owner" data-chat="owner">Контроль диалогов сотрудников</button>':''}<button class="pb-chat-link" data-chat="rules">Общие правила</button>`);
 }catch(e){showError(e.message);}
}
function renderMessages(items,{readonly=false}={}){
 const box=dialog.querySelector('#pbChatMessages'); if(!box)return;
 for(const m of items){
  if(box.querySelector(`[data-message-id="${m.id}"]`))continue;
  const mine=m.senderId===me?.user?.id;
  const el=document.createElement('article');el.className='pb-chat-message '+(mine?'mine':'');el.dataset.messageId=m.id;
  el.innerHTML=`<div class="pb-chat-meta">${esc(m.senderName)} · ${esc(time(m.createdAt))}</div><div class="pb-chat-bubble">${esc(m.body).replace(/\n/g,'<br>')}</div>`;
  box.append(el); last=Math.max(last,m.id);
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
   renderMessages(data.messages,{readonly:true});
  }else{
   const peer=current.kind==='general'?'general':current.peer;
   const data=await api(`/chat/messages?peer=${peer}&after=${last}`);
   renderMessages(data.messages);
  }
 }catch(e){showError(e.message);}finally{busy=false;}
}
async function openThread(kind,peer=null,title='Общий чат'){
 current={kind,peer,title};last=0;stopPoll();
 shell(title,`<div id="pbChatMessages" class="pb-chat-messages" aria-live="polite"></div><p class="pb-chat-error" data-chat-error hidden role="alert"></p><form class="pb-chat-compose" id="pbChatCompose"><textarea name="body" maxlength="2000" rows="2" placeholder="Рабочее сообщение…" required></textarea><button class="pb-chat-send" type="submit">Отправить</button></form>`);
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
dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
dialog.addEventListener('click',async e=>{
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
 const field=e.target.elements.body;const body=field.value.trim();if(!body)return;
 const button=e.target.querySelector('[type=submit]');button.disabled=true;
 try{const peerId=current.kind==='general'?null:current.peer;const d=await api('/chat/messages',{method:'POST',body:{peerId,body}});field.value='';renderMessages([d.message]);}
 catch(err){showError(err.message);}finally{button.disabled=false;field.focus();}
});
function inject(){
 const top=document.querySelector('#topbar');if(!top||top.querySelector('[data-open-chat]'))return;
 const btn=document.createElement('button');btn.className='pb-chat-trigger';btn.dataset.openChat='1';btn.textContent='Чат';btn.setAttribute('aria-label','Рабочий чат');
 btn.addEventListener('click',home);top.append(btn);
}
async function boot(){try{me=await api('/me');inject();}catch{me=null;}}
const observer=new MutationObserver(()=>{if(me)inject();else if(document.querySelector('#topbar')?.children.length)boot();});
observer.observe(document.querySelector('#topbar'),{childList:true});boot();
