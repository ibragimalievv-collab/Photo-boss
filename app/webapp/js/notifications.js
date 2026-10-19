import {api} from './api.js';
import {esc} from './domain.js';
let state=null,account=null,revision=0;
const supported=()=>isSecureContext&&'serviceWorker' in navigator&&'PushManager' in window&&'Notification' in window;
const installed=()=>matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
const ios=()=>/iPhone|iPad|iPod/.test(navigator.userAgent);
const disabled=()=>localStorage.getItem('pb-push-disabled')==='1';
function bytes(key){const raw=atob(key.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-key.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
async function registration(){return navigator.serviceWorker.register('/app/sw.js',{scope:'/app/'}).then(()=>navigator.serviceWorker.ready);}
export async function syncPushAccount(me){
 const ticket=++revision;account=me.user.id;
 if(!supported()||Notification.permission!=='granted'||disabled())return;
 try{const next=await api('/push',{allowSnapshot:false});if(ticket!==revision||!next.configured)return;state=next;
 const worker=await registration(),sub=await worker.pushManager.getSubscription();
 if(ticket!==revision)return;
 if(sub)await api('/push/subscribe',{method:'POST',body:{subscription:sub.toJSON()}});
 }catch{/* No prompt or false success on a failed background refresh. */}
}
export async function detachPush(){
 revision++;account=null;if(!('serviceWorker' in navigator))return;
 const worker=await navigator.serviceWorker.getRegistration('/app/'),sub=await worker?.pushManager.getSubscription();
 if(!sub)return;
 try{await api('/push/unsubscribe',{method:'POST',body:{endpoint:sub.endpoint}});}finally{await sub.unsubscribe();}
}
export async function notificationsView(){
 state=await api('/push',{allowSnapshot:false});let subscribed=false;
 if(supported()){const worker=await navigator.serviceWorker.getRegistration('/app/');subscribed=!!await worker?.pushManager.getSubscription();}
 const hint=ios()&&!installed()?'На iPhone: откройте Photo Boss в Safari → Поделиться → На экран «Домой». Затем запустите с иконки и включите уведомления.':!supported()?'В этом браузере системные уведомления недоступны. Откройте установленное приложение или поддерживаемый браузер.':Notification.permission==='denied'?'Доступ запрещён в настройках браузера или телефона. Разрешите уведомления Photo Boss в настройках и откройте приложение снова.':subscribed?'Это устройство подписано на уведомления.':'Разрешите уведомления для этого устройства. Разрешение спрашивается один раз.';
 return `<section id="appNotifications" class="section"><p>Рабочие события сохраняются в Photo Boss. ${state.channel==='app'?'Telegram для этих уведомлений отключён.':'Переключение доставки в приложение ещё не активировано на сервере.'}</p><p>${esc(hint)}</p><p>${state.configured?'Сервер push-уведомлений настроен.':'Системные push ещё настраиваются; список событий доступен ниже.'}</p><div class="row wrap"><button class="btn primary" data-push="enable" ${!supported()||!state.configured||(ios()&&!installed())?'disabled':''}>Включить уведомления</button><button class="btn ghost" data-push="test" ${!subscribed?'disabled':''}>Проверить уведомление</button><button class="btn ghost" data-push="disable" ${!subscribed?'disabled':''}>Отключить на этом устройстве</button></div><p id="pushStatus" role="status"></p><h3>Мои уведомления</h3>${state.items.map(n=>`<a class="panel section" style="display:block" href="${esc(n.url)}">${esc(n.title)}<br><small>${esc(new Date(n.createdAt).toLocaleString('ru-RU'))}</small></a>`).join('')||'<p>Новых событий пока нет.</p>'}</section>`;
}
document.addEventListener('click',async event=>{
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
   localStorage.setItem('pb-push-disabled','1');const current=account;await detachPush();account=current;
   status.textContent='Уведомления на этом устройстве отключены. История остаётся в Photo Boss.';
  }else{
   await api('/push/test',{method:'POST',body:{}});status.textContent='Проверка поставлена в очередь. Сверните приложение и дождитесь системного уведомления. Это ещё не подтверждение доставки на телефон.';
  }
 }catch(error){if(status)status.textContent=error.message;}finally{button.disabled=false;}
});
