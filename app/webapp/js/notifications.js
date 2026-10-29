import {api} from './api.js';
import {esc} from './domain.js';
let state=null,account=null,revision=0,feedVersion=0,mutationVersion=0,feedTimer=null,feedReady=false;
let seenNotices=new Set(),loadingOlder=false,paginationVersion=0,readChain=Promise.resolve();
const pendingReads=new Set();let readingAll=false,feedback='';
const pushStateText={OFF:'Push отключён; уведомление сохранено в приложении.',NO_DEVICE:'Нет подключённого устройства; уведомление сохранено в приложении.',PENDING:'Push ожидает отправки; уведомление уже сохранено в приложении.',ACCEPTED:'Push-сервер принял отправку, показ на телефоне не подтверждён; уведомление сохранено в приложении.',PARTIAL:'Push принят не всеми устройствами; уведомление сохранено в приложении.',FAILED:'Push не отправлен; уведомление сохранено в приложении. Проверьте разрешение устройства.',EXPIRED:'Время напоминания истекло; новые push не отправляются. Уведомление сохранено в приложении.',HISTORY_ONLY:'Устройство подключено позже события; push для прежних событий не отправляется. Уведомление сохранено в приложении.',SKIPPED:'Push пропущен, так как событие уже неактуально; уведомление сохранено в приложении.'};

const stripBrand=value=>String(value||'').replace(/^Photo\s*Boss\s*[·:—-]?\s*/i,'').trim();
export function notificationActionText(notice){
 if(notice.kind==='attendance_fine')return notice.url?.startsWith('/app/#more?attendanceFine=')?'Проверить и исправить начисление':'Посмотреть мои начисления';
 return ({booking_assignment:'Открыть съёмку',booking_reminder:'Открыть съёмку',sale_ready:'Перейти к продаже',sale_created:'Посмотреть продажу',sale_discount:'Проверить скидку',chat:'Открыть переписку',call:'Открыть звонок',academy:'Открыть разбор',attendance:'Открыть смену',lunch_attention:'Проверить отметку',operational_alert:'Проверить продажу',service_status:'Проверить подключение'})[notice.kind]||'Открыть событие';
}
export function notificationSummary(notice){
 const lines=String(notice.description||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
 const booking=lines.some(line=>line.startsWith('Отель:'))&&lines.some(line=>line.startsWith('Дата:'));
 const text=booking?['Дата:','Отель:','Гость:','Комната:'].map(prefix=>lines.find(line=>line.startsWith(prefix))?.slice(prefix.length).trim()).filter(Boolean).join(' · '):lines.join(' · ');
 return text.length>190?text.slice(0,187).trimEnd()+'…':text;
}
export function notificationToast(notice){const title=stripBrand(notice.title)||'Рабочее событие',summary=notificationSummary(notice);return title+(summary?' · '+summary:'');}
export function mergeNotificationItems(existing,incoming){
 const byId=new Map();for(const row of [...existing,...incoming])if(Number.isSafeInteger(row?.id)&&row.id>0)byId.set(row.id,row);
 const keys=new Set();return [...byId.values()].sort((a,b)=>b.id-a.id).filter(row=>{const key=typeof row.eventKey==='string'&&row.eventKey?row.eventKey:null;if(key&&keys.has(key))return false;if(key)keys.add(key);return true;});
}
const unread=()=>Number.isSafeInteger(state?.unreadCount)&&state.unreadCount>=0?state.unreadCount:null;
export function refreshNotificationBadge(){
 const button=document.querySelector('[data-action="app-notifications"]');if(!button?.querySelector)return;
 const count=unread();button.setAttribute('aria-label',count?`Уведомления. Непрочитанных: ${count}`:'Уведомления');
 let badge=button.querySelector('[data-notice-count]');if(!badge){badge=document.createElement('span');badge.dataset.noticeCount='1';badge.className='notification-count';badge.setAttribute('aria-hidden','true');button.append(badge);}
 badge.hidden=!count;badge.textContent=count?String(count):'';
}
function safeNoticeUrl(raw){try{const url=new URL(raw,location.origin);if(url.origin===location.origin&&url.pathname==='/app/'&&url.hash)return url.pathname+url.search+url.hash;}catch{}return '/app/#home';}
function noticeTime(value){
 const date=new Date(value);if(!Number.isFinite(date.getTime()))return '<span class="notification-time">Время не указано</span>';
 const full=new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'}).format(date);
 return `<time class="notification-time" datetime="${esc(date.toISOString())}" title="${esc(full)} · Москва">${esc(full)}</time>`;
}
function noticeCard(row){
 const title=stripBrand(row.title)||'Рабочее событие',summary=notificationSummary(row),priority=['high','critical','urgent'].includes(String(row.priority).toLowerCase());
 const delivery=pushStateText[row.pushState]||'Уведомление сохранено в приложении.';
 const telegram=row.ownerTelegramState?' Telegram: '+({ACCEPTED:'принято сервисом; прочтение не подтверждено',PENDING:'ожидает отправки',FAILED:'не отправлено',SKIPPED:'событие неактуально',HISTORY_ONLY:'история до подключения копий'}[row.ownerTelegramState]||row.ownerTelegramState)+'.':'';
 return `<li><article class="notification-card ${row.read?'is-read':'is-unread'} ${priority?'is-important':''}" data-notice-id="${row.id}" aria-labelledby="notice-title-${row.id}">
 <div class="notification-meta"><span class="notification-read-state">${row.read?'Прочитано':'Новое'}</span>${priority?'<span class="notification-priority">Требует внимания</span>':''}${noticeTime(row.createdAt)}</div>
 <h3 id="notice-title-${row.id}">${esc(title)}</h3>${summary?`<p class="notification-summary">${esc(summary)}</p>`:''}
 <div class="notification-actions"><a class="btn ghost" data-notice-read="${row.id}" data-notice-control="open-${row.id}" href="${esc(safeNoticeUrl(row.url))}" aria-label="${esc(notificationActionText(row)+': '+title)}">${esc(notificationActionText(row))}</a>${!row.read?`<button type="button" class="notification-mark" data-notice-mark="${row.id}" data-notice-control="mark-${row.id}" ${pendingReads.has(row.id)||readingAll?'disabled':''} aria-label="Отметить прочитанным: ${esc(title)}">${pendingReads.has(row.id)?'Сохраняем…':'Прочитано'}</button>`:''}</div>
 <details class="notification-details" data-notice-details="${row.id}"><summary data-notice-control="details-${row.id}">Подробности</summary><p class="notification-full-text">${esc(row.description||'Дополнительных сведений нет.')}</p><p class="notification-delivery">${esc(delivery+telegram)}</p></details>
 </article></li>`;
}
function inboxContents(){
 const count=unread(),items=state?.items||[],latest=state?.latestId;
 return `<div class="notification-toolbar"><p class="notification-total">${count===null?'Обновляем счётчик…':count?`Непрочитанных: ${count}`:'Всё прочитано'}</p><div class="notification-toolbar-actions"><button type="button" class="btn ghost" data-notice-action="refresh" data-notice-control="refresh">Обновить</button><button type="button" class="btn ghost" data-notice-action="read-all" data-notice-control="read-all" ${!count||!latest||readingAll||pendingReads.size?'disabled':''}>${readingAll?'Сохраняем…':'Прочитать всё'}</button></div></div>
 <p class="notification-feedback" data-notice-feedback role="status" aria-live="polite">${esc(feedback)}</p>
 ${items.length?`<ol class="notification-list" aria-label="Рабочие события">${items.map(noticeCard).join('')}</ol>`:'<div class="notification-empty"><strong>Пока нет уведомлений</strong><p>Новые рабочие события появятся здесь.</p></div>'}
 <div class="notification-pagination"><span>Показано событий: ${items.length}</span>${state?.hasMore?`<button type="button" class="btn ghost" data-notice-action="older" data-notice-control="older" aria-disabled="${loadingOlder?'true':'false'}">${loadingOlder?'Загружаем…':'Показать более ранние'}</button>`:''}</div>`;
}
function renderInbox(message=null){
 if(message!==null)feedback=message;
 const box=document.querySelector('#appNotifications [data-notice-feed]');if(!box?.querySelectorAll)return;
 const expanded=[...box.querySelectorAll('[data-notice-details][open]')].map(node=>node.dataset.noticeDetails),active=document.activeElement,focused=box.contains(active)?active?.dataset.noticeControl:null;
 const sheet=box.closest('dialog'),scroll=sheet?.scrollTop;
 box.innerHTML=inboxContents();for(const id of expanded){const details=box.querySelector(`[data-notice-details="${id}"]`);if(details)details.open=true;}

 if(focused){let target=box.querySelector(`[data-notice-control="${focused}"]`);if(!target||target.disabled)target=focused.startsWith('mark-')?box.querySelector(`[data-notice-control="open-${focused.slice(5)}"]`):box.querySelector('[data-notice-control="refresh"]');target?.focus({preventScroll:true});}
 if(sheet&&scroll!==undefined)sheet.scrollTop=scroll;
}
function acceptFresh(next,{reset=false}={}){
 const previous=state?.items||[],incoming=mergeNotificationItems([],next.items||[]),overlap=incoming.some(row=>previous.some(old=>old.id===row.id));
 const replace=reset||!state||!next.hasMore,restart=replace||!overlap;
 const items=replace?incoming:mergeNotificationItems(previous,incoming);if(reset)feedback='';
 state={...next,items,hasMore:restart?Boolean(next.hasMore):Boolean(state.hasMore),olderCursor:restart?next.olderCursor:state.olderCursor};
 if(restart)paginationVersion++;
 refreshNotificationBadge();renderInbox();
 for(const notice of incoming)if(!notice.read&&!seenNotices.has(notice.id)&&feedReady&&!['EXPIRED','SKIPPED'].includes(notice.pushState))document.dispatchEvent(new CustomEvent('pb-notification',{detail:notice}));
 for(const notice of incoming)seenNotices.add(notice.id);feedReady=true;
}
async function refreshNoticeFeed({reset=false}={}){
 if(account===null||document.hidden)return null;const ticket=revision,version=++feedVersion,mutation=mutationVersion;
 try{const next=await api('/push?limit=12',{allowSnapshot:false});if(ticket!==revision||version!==feedVersion||mutation!==mutationVersion)return null;acceptFresh(next,{reset});return state;}
 catch(error){if(ticket===revision&&reset)throw error;return null;}
}
async function loadOlder(){
 if(loadingOlder||!state?.hasMore||!state.olderCursor)return;loadingOlder=true;renderInbox();
 const ticket=revision,mutation=mutationVersion,page=paginationVersion,cursor=state.olderCursor,fresh=feedVersion;
 try{
  const next=await api('/push?limit=12&before='+encodeURIComponent(cursor),{allowSnapshot:false});if(ticket!==revision||mutation!==mutationVersion)return;
  state={...state,items:mergeNotificationItems(state.items,next.items||[]),...(page===paginationVersion?{hasMore:Boolean(next.hasMore),olderCursor:next.olderCursor}:{}),...(fresh===feedVersion?{unreadCount:next.unreadCount}: {})};refreshNotificationBadge();
 }catch(error){if(ticket===revision)renderInbox(error.message);return;}
 finally{if(ticket===revision){loadingOlder=false;renderInbox();}}
}
function acknowledge(id=null){
 const ticket=revision,throughId=state?.latestId;if(id===null){if(readingAll||!throughId)return Promise.resolve();readingAll=true;}else{if(pendingReads.has(id))return Promise.resolve();pendingReads.add(id);}
 mutationVersion++;feedback='';renderInbox();
 const work=readChain.catch(()=>{}).then(async()=>{
  if(ticket!==revision)return;mutationVersion++;
  const result=await api(id===null?'/push/read-all':'/push/read',{method:'POST',body:id===null?{throughId}:{id}});
  if(ticket!==revision)return;mutationVersion++;
  for(const row of state?.items||[])if(id===null?row.id<=throughId:row.id===id)row.read=true;
  if(Number.isSafeInteger(result.unreadCount)&&result.unreadCount>=0)state.unreadCount=result.unreadCount;
  refreshNotificationBadge();await refreshNoticeFeed();if(ticket===revision)feedback='Прочтение сохранено.';
 }).catch(error=>{if(ticket===revision)renderInbox('Не удалось сохранить прочтение. '+error.message);}).finally(()=>{
  if(ticket!==revision)return;if(id===null)readingAll=false;else pendingReads.delete(id);renderInbox();
 });readChain=work;return work;
}
const supported=()=>isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
const installed=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const ios=()=>/iPhone|iPad|iPod/.test(navigator.userAgent);
const disabled=()=>localStorage.getItem('pb-push-disabled')==='1';
function bytes(key){const raw=atob(key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-key.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
async function registration(){return navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).then(()=>navigator.serviceWorker.ready);}

export async function syncPushAccount(me){
 const previous=account,ticket=++revision;account=me.user.id;
 if(previous!==account){state=null;feedReady=false;feedback='';seenNotices=new Set();pendingReads.clear();readingAll=false;readChain=Promise.resolve();loadingOlder=false;refreshNotificationBadge();}
 if(typeof clearInterval==='function'&&feedTimer)clearInterval(feedTimer);const initial=refreshNoticeFeed();if(typeof setInterval==='function')feedTimer=setInterval(()=>refreshNoticeFeed(),15000);
 if(!supported()||Notification.permission!=='granted'||disabled())return;
 try{const next=await initial;if(ticket!==revision||!next?.configured)return;const worker=await registration();let sub=await worker.pushManager.getSubscription();
 if(ticket!==revision)return;if(!sub)sub=await worker.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(next.publicKey)});if(ticket!==revision){await sub.unsubscribe();return;}if(sub)await api('/push/subscribe',{method:'POST',body:{subscription:sub.toJSON()}});
 }catch{/* A failed device subscription does not prevent reading the inbox. */}
}
async function unsubscribeDevice(){
 if(!('serviceWorker' in navigator))return;
 const worker=await navigator.serviceWorker.getRegistration('/app/'),sub=await worker?.pushManager.getSubscription();
 if(!sub)return;
 try{await api('/push/unsubscribe',{method:'POST',body:{endpoint:sub.endpoint}});}finally{await sub.unsubscribe();}
}

export async function detachPush(){
 revision++;account=null;state=null;seenNotices=new Set();feedReady=false;feedback='';pendingReads.clear();readingAll=false;loadingOlder=false;readChain=Promise.resolve();if(typeof clearInterval==='function'&&feedTimer)clearInterval(feedTimer);feedTimer=null;refreshNotificationBadge();await unsubscribeDevice();
}
export async function notificationsView(){
 const ticket=revision;await refreshNoticeFeed({reset:true});if(ticket!==revision||!state)throw Error('Аккаунт изменился. Откройте уведомления заново.');let subscribed=false;
 if(supported()){const worker=await navigator.serviceWorker.getRegistration('/app/');subscribed=!!await worker?.pushManager.getSubscription();}if(ticket!==revision)throw Error('Аккаунт изменился.');
 const hint=ios()&&!installed()?'На iPhone: откройте Photo Boss в Safari → Поделиться → На экран «Домой». Затем запустите с иконки и включите уведомления.':!supported()?'В этом браузере системные уведомления недоступны. История событий доступна здесь.':Notification.permission==='denied'?'Доступ запрещён в настройках браузера или телефона. Разрешите уведомления Photo Boss в настройках.':subscribed?'Это устройство подписано на уведомления.':'Разрешите уведомления для этого устройства. Разрешение спрашивается один раз.';
 return `<section id="appNotifications" class="notification-center"><div data-notice-feed>${inboxContents()}</div><details class="notification-channel-settings"><summary>Каналы уведомлений</summary><p>Рабочие события всегда доступны в приложении. Разрешение телефона для их просмотра не требуется.</p>${state.canConfigureOwnerTelegram?`<section><h3>Копии владельцу в Telegram</h3><p>Только новые события и только для вашего аккаунта.</p><button type="button" class="btn ghost" data-owner-telegram="toggle">${state.ownerTelegram?'Отключить копии в мой Telegram':'Включить копии в мой Telegram'}</button><button type="button" class="btn ghost" data-owner-telegram="test" ${state.ownerTelegram?'':'disabled'}>Проверить уведомление в приложении и Telegram</button><p id="ownerTelegramStatus" role="status"></p></section>`:''}<details class="section"><summary>Уведомления на устройстве — дополнительно</summary><p>${esc(hint)}</p><p>${state.configured?'Сервер push-уведомлений настроен.':'Системные push ещё настраиваются; история событий доступна.'}</p><div class="row wrap"><button type="button" class="btn primary" data-push="enable" ${!supported()||!state.configured||(ios()&&!installed())?'disabled':''}>Включить уведомления</button><button type="button" class="btn ghost" data-push="test" ${!subscribed?'disabled':''}>Проверить уведомление</button><button type="button" class="btn ghost" data-push="disable" ${!subscribed?'disabled':''}>Отключить на этом устройстве</button></div><p id="pushStatus" role="status"></p><p>Подключённых устройств: ${Number(state.activeDevices||0)}.</p></details></details></section>`;
}
document.addEventListener('click',async event=>{
 const ownerButton=event.target.closest('[data-owner-telegram]');
 if(ownerButton?.dataset.ownerTelegram){const ticket=revision,status=document.querySelector('#ownerTelegramStatus');ownerButton.disabled=true;
  try{if(ownerButton.dataset.ownerTelegram==='toggle'){
   const enabled=!state.ownerTelegram;await api('/push/owner-telegram',{method:'POST',body:{enabled}});
   if(ticket!==revision)return;state.ownerTelegram=enabled;
   ownerButton.textContent=enabled?'Отключить копии в мой Telegram':'Включить копии в мой Telegram';
   document.querySelector('[data-owner-telegram="test"]').disabled=!enabled;
   status.textContent=enabled?'Копии включены для вашего Telegram. Можно отправить проверочное уведомление.':'Копии отключены. Уведомления остаются в приложении.';
  }else{await api('/push/test',{method:'POST',body:{}});status.textContent='Проверка сохранена в очереди. Статус отправки Telegram появится у события; прочтение на телефоне не подтверждается автоматически.';}
  }catch(error){if(status)status.textContent=error.message;}finally{ownerButton.disabled=false;}return;
 }


 const action=event.target.closest('[data-notice-action]');if(action?.dataset.noticeAction){action.disabled=true;try{if(action.dataset.noticeAction==='older')await loadOlder();else if(action.dataset.noticeAction==='read-all')await acknowledge();else await refreshNoticeFeed({reset:true});}catch(error){renderInbox(error.message);}finally{if(action.isConnected)action.disabled=false;}return;}
 const mark=event.target.closest('[data-notice-mark]');if(mark?.dataset.noticeMark){await acknowledge(Number(mark.dataset.noticeMark));return;}
 const link=event.target.closest('[data-notice-read]');if(link?.dataset.noticeRead){event.preventDefault();const url=safeNoticeUrl(link.getAttribute('href'));document.dispatchEvent(new CustomEvent('pb-notification-open',{detail:{url}}));await acknowledge(Number(link.dataset.noticeRead));return;}
 const button=event.target.closest('[data-push]');if(!button)return;button.disabled=true;
 const status=document.querySelector('#pushStatus'),ticket=revision;
 try{
  if(button.dataset.push==='enable'){
   // This call must happen directly in the user's click, before any fetch.
   const permission=await Notification.requestPermission();if(permission!=='granted')throw Error('Уведомления не разрешены. События остаются в приложении.');
   const worker=await registration();let sub=await worker.pushManager.getSubscription();
   if(!sub)sub=await worker.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(state.publicKey)});
   if(ticket!==revision||account===null){await sub.unsubscribe();return;}
   await api('/push/subscribe',{method:'POST',body:{subscription:sub.toJSON()}});localStorage.removeItem('pb-push-disabled');
   status.textContent='Устройство подписано. Нажмите «Проверить уведомление», затем сверните приложение.';
   document.querySelectorAll('[data-push="test"],[data-push="disable"]').forEach(b=>b.disabled=false);
  }else if(button.dataset.push==='disable'){
   localStorage.setItem('pb-push-disabled','1');await unsubscribeDevice();
   status.textContent='Уведомления на этом устройстве отключены. История остаётся в Photo Boss.';
  }else{
   await api('/push/test',{method:'POST',body:{}});status.textContent='Проверка поставлена в очередь. Сверните приложение и дождитесь системного уведомления. Это ещё не подтверждение доставки на телефон.';
  }
 }catch(error){if(status)status.textContent=error.message;}finally{button.disabled=false;}
});


document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshNoticeFeed();});
