import {api} from './api.js';
import {esc} from './domain.js';
let state=null,account=null,revision=0,feedTimer=null,feedReady=false;
let seenNotices=new Set();
const pushStateText={OFF:'Push отключён; уведомление сохранено в приложении.',NO_DEVICE:'Нет подключённого устройства; уведомление сохранено в приложении.',PENDING:'Push ожидает отправки; уведомление уже сохранено в приложении.',ACCEPTED:'Push-сервер принял отправку, показ на телефоне не подтверждён; уведомление сохранено в приложении.',PARTIAL:'Push принят не всеми устройствами; уведомление сохранено в приложении.',FAILED:'Push не отправлен; уведомление сохранено в приложении. Проверьте разрешение устройства.',EXPIRED:'Время напоминания истекло; новые push не отправляются. Уведомление сохранено в приложении.',HISTORY_ONLY:'Устройство подключено позже события; push для прежних событий не отправляется. Уведомление сохранено в приложении.',SKIPPED:'Push пропущен, так как событие уже неактуально; уведомление сохранено в приложении.'};
export function notificationActionText(notice){return notice.kind==='attendance_fine'?(notice.url?.startsWith('/app/#more?attendanceFine=')?'Проверить и исправить начисление':'Посмотреть мои начисления'):'Открыть событие';}
export function refreshNotificationBadge(){const button=document.querySelector('[data-action="app-notifications"]');if(!button?.querySelector)return;const count=(state?.items||[]).filter(n=>!n.read).length;button.setAttribute('aria-label',count?`Уведомления: ${count} новых`:'Уведомления');let badge=button.querySelector('[data-notice-count]');if(!badge){badge=document.createElement('span');badge.dataset.noticeCount='1';badge.className='notification-count';button.append(badge);}badge.hidden=!count;badge.textContent=count?String(count):'';}
async function refreshNoticeFeed(){const ticket=revision;if(account===null||document.hidden)return;try{const next=await api('/push',{allowSnapshot:false});if(ticket!==revision)return;state=next;refreshNotificationBadge();for(const notice of next.items||[])if(!notice.read&&!seenNotices.has(notice.id)&&feedReady&&!['EXPIRED','SKIPPED'].includes(notice.pushState))document.dispatchEvent(new CustomEvent('pb-notification',{detail:notice}));seenNotices=new Set((next.items||[]).map(n=>n.id));feedReady=true;}catch{/* Retry on the next foreground refresh. */}}
const supported=()=>isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
const installed=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const ios=()=>/iPhone|iPad|iPod/.test(navigator.userAgent);
const disabled=()=>localStorage.getItem('pb-push-disabled')==='1';
function bytes(key){const raw=atob(key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-key.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
async function registration(){return navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).then(()=>navigator.serviceWorker.ready);}
export async function syncPushAccount(me){
 const previous=account,ticket=++revision;account=me.user.id;if(previous!==account){state=null;feedReady=false;seenNotices=new Set();}if(typeof clearInterval==='function'&&feedTimer)clearInterval(feedTimer);if(typeof setInterval==='function'){refreshNoticeFeed();feedTimer=setInterval(refreshNoticeFeed,15000);}
 if(!supported()||Notification.permission!=='granted'||disabled())return;
 try{const next=await api('/push',{allowSnapshot:false});if(ticket!==revision||!next.configured)return;state=next;
 const worker=await registration();let sub=await worker.pushManager.getSubscription();
 if(ticket!==revision)return;
 if(!sub)sub=await worker.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(next.publicKey)});if(ticket!==revision){await sub.unsubscribe();return;}if(sub)await api('/push/subscribe',{method:'POST',body:{subscription:sub.toJSON()}});
 }catch{/* No prompt or false success on a failed background refresh. */}
}
async function unsubscribeDevice(){
 if(!('serviceWorker' in navigator))return;
 const worker=await navigator.serviceWorker.getRegistration('/app/'),sub=await worker?.pushManager.getSubscription();
 if(!sub)return;
 try{await api('/push/unsubscribe',{method:'POST',body:{endpoint:sub.endpoint}});}finally{await sub.unsubscribe();}
}
export async function detachPush(){
 revision++;account=null;state=null;seenNotices=new Set();feedReady=false;if(typeof clearInterval==='function'&&feedTimer)clearInterval(feedTimer);feedTimer=null;refreshNotificationBadge();await unsubscribeDevice();
}
export async function notificationsView(){
 state=await api('/push',{allowSnapshot:false});refreshNotificationBadge();let subscribed=false;
 if(supported()){const worker=await navigator.serviceWorker.getRegistration('/app/');subscribed=!!await worker?.pushManager.getSubscription();}
 const hint=ios()&&!installed()?'На iPhone: откройте Photo Boss в Safari → Поделиться → На экран «Домой». Затем запустите с иконки и включите уведомления.':!supported()?'В этом браузере системные уведомления недоступны. Откройте установленное приложение или поддерживаемый браузер.':Notification.permission==='denied'?'Доступ запрещён в настройках браузера или телефона. Разрешите уведомления Photo Boss в настройках и откройте приложение снова.':subscribed?'Это устройство подписано на уведомления.':'Разрешите уведомления для этого устройства. Разрешение спрашивается один раз.';
 return `<section id="appNotifications" class="section"><p>Рабочие уведомления приходят сюда, в Photo Boss. Для просмотра не требуется разрешение уведомлений телефона. ${state.ownerTelegram?'Владельцу дополнительно отправляется копия новых событий в Telegram.':state.channel==='app'?'Telegram не заменяет уведомления сотрудников в приложении.':''}</p>${state.canConfigureOwnerTelegram?`<section><h3>Telegram для моего аккаунта владельца</h3><p>Копии новых событий отправляются только в Telegram аккаунта, привязанного к вашему входу. Старые события не рассылаются.</p><button class="btn ghost" data-owner-telegram="toggle">${state.ownerTelegram?'Отключить копии в мой Telegram':'Включить копии в мой Telegram'}</button><button class="btn ghost" data-owner-telegram="test" ${state.ownerTelegram?'':'disabled'}>Проверить уведомление в приложении и Telegram</button><p id="ownerTelegramStatus" role="status"></p></section>`:''}<h3>Мои уведомления</h3>${state.items.map(n=>`<a class="panel section" style="display:block" data-notice-read="${n.id}" href="${esc(n.url)}"><strong>${esc(n.title)}</strong><br><span style="white-space:pre-line">${esc(n.description||'')}</span><br><small>${esc(new Date(n.createdAt).toLocaleString('ru-RU'))} · ${esc(pushStateText[n.pushState]||'Уведомление сохранено в приложении.')}${n.ownerTelegramState?' · Telegram: '+esc({ACCEPTED:'принято Telegram (прочтение не подтверждено)',PENDING:'ожидает отправки',FAILED:'не отправлено',SKIPPED:'пропущено — событие неактуально',HISTORY_ONLY:'история до подключения копий'}[n.ownerTelegramState]||n.ownerTelegramState):''}</small><br><span>${esc(notificationActionText(n))}</span></a>`).join('')||'<p>Новых событий пока нет.</p>'}<details class="section"><summary>Уведомления на устройстве — дополнительно</summary><p>${esc(hint)}</p><p>${state.configured?'Сервер push-уведомлений настроен.':'Системные push ещё настраиваются; история событий уже доступна в приложении.'}</p><div class="row wrap"><button class="btn primary" data-push="enable" ${!supported()||!state.configured||(ios()&&!installed())?'disabled':''}>Включить уведомления</button><button class="btn ghost" data-push="test" ${!subscribed?'disabled':''}>Проверить уведомление</button><button class="btn ghost" data-push="disable" ${!subscribed?'disabled':''}>Отключить на этом устройстве</button></div><p id="pushStatus" role="status"></p><p>Подключённых устройств аккаунта: ${Number(state.activeDevices||0)}.</p></details></section>`;
}
document.addEventListener('click',async event=>{
 const ownerButton=event.target.closest('[data-owner-telegram]');
 if(ownerButton){const ticket=revision,status=document.querySelector('#ownerTelegramStatus');ownerButton.disabled=true;
  try{if(ownerButton.dataset.ownerTelegram==='toggle'){
   const enabled=!state.ownerTelegram;await api('/push/owner-telegram',{method:'POST',body:{enabled}});
   if(ticket!==revision)return;state.ownerTelegram=enabled;
   ownerButton.textContent=enabled?'Отключить копии в мой Telegram':'Включить копии в мой Telegram';
   document.querySelector('[data-owner-telegram="test"]').disabled=!enabled;
   status.textContent=enabled?'Копии включены для вашего Telegram. Можно отправить проверочное уведомление.':'Копии отключены. Уведомления остаются в приложении.';
  }else{await api('/push/test',{method:'POST',body:{}});status.textContent='Проверка сохранена в очереди. Статус отправки Telegram появится у события; прочтение на телефоне не подтверждается автоматически.';}
  }catch(error){if(status)status.textContent=error.message;}finally{ownerButton.disabled=false;}return;
 }

 const link=event.target.closest('[data-notice-read]');if(link?.dataset.noticeRead){event.preventDefault();document.dispatchEvent(new CustomEvent('pb-notification-open',{detail:{url:link.getAttribute('href')}}));const id=Number(link.dataset.noticeRead),ticket=revision;try{await api('/push/read',{method:'POST',body:{id}});if(ticket!==revision)return;for(const row of state?.items||[])if(row.id===id)row.read=true;refreshNotificationBadge();}catch{}return;}
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
