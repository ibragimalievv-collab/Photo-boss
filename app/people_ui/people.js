import {api} from '/app/js/api.js';
import {esc,ROLE_NAMES} from '/app/js/domain.js';
import {icon} from '/app/js/icons.js';
const standaloneMode=()=>window.PHOTO_BOSS_CONFIG?.STANDALONE===true;
const employeeIdentity=employee=>standaloneMode()?employee.id:employee.telegramId;
function identityField(employee){
 if(standaloneMode())return employee?`<p class="pb-people-muted">Номер сотрудника: ${esc(employee.id)}. Аккаунт и история сохраняются.</p>`:'<p class="pb-people-muted">После сохранения открой карточку сотрудника и отправь ему персональный QR для входа.</p>';
 return !employee?'<label>Telegram ID<input name="telegramId" inputmode="numeric" pattern="[0-9]+" required autocomplete="off"></label><p class="pb-people-muted">Telegram ID указан в профиле Photo Boss или на экране проверки доступа. Имя пользователя @username не заменяет ID. После добавления сотрудник может повторить вход в приложение.</p>':`<p class="pb-people-muted">Telegram ID: ${esc(employee.telegramId)}. Привязку аккаунта эта форма не меняет.</p>`;
}
const dialog=document.createElement('dialog');dialog.className='pb-people-dialog';dialog.setAttribute('aria-labelledby','pbPeopleTitle');document.body.append(dialog);
const css=document.createElement('link');css.rel='stylesheet';css.href='/people/people.css';document.head.append(css);
let me=null,listing=null,requestId=0,previousFocus=null,archived=false;
let currentQr=null,qrRequestId=0;
const labels=roles=>(roles||[]).map(x=>ROLE_NAMES[x]||x).join(' · ')||'Роль не назначена';
const formatLogin=value=>value?new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(value+'Z')):'Ещё не входил';
function clearQr(){qrRequestId++;currentQr=null;}
function screen(title,html,keepQr=false){if(!keepQr)clearQr();dialog.innerHTML=`<header class="pb-people-head"><h2 id="pbPeopleTitle">${esc(title)}</h2><button class="pb-people-close" data-people="close" aria-label="Закрыть">×</button></header><div class="pb-people-inner">${html}</div>`;if(!dialog.open){previousFocus=document.activeElement;dialog.showModal();}dialog.scrollTop=0;}
function close(){requestId++;clearQr();dialog.close();dialog.replaceChildren();previousFocus?.focus?.();previousFocus=null;}
function error(e){if([401,403].includes(e.status)){listing=null;screen('Доступ недоступен',`<p role="alert">${esc(e.message)}</p><p>${standaloneMode()?'Войди заново по персональному QR или логину и паролю.':'Закройте приложение и откройте его заново через Telegram.'}</p>`);return;}const el=dialog.querySelector('[data-form-error]');if(el){el.textContent=e.message;el.hidden=false;}else screen('Не удалось загрузить',`<p role="alert">${esc(e.message)}</p><button class="pb-people-button" data-people="list">Повторить</button>`);}
async function openList(more=false,showArchived=archived){archived=showArchived;const ticket=++requestId;if(!more)screen(archived?'Уволенные сотрудники':'Сотрудники','<p role="status">Загружаем список…</p>');try{const params=new URLSearchParams();if(archived)params.set('archived','1');if(more&&listing?.next)params.set('after',listing.next);const qs=params.toString();const data=await api('/people'+(qs?'?'+qs:''));if(ticket!==requestId)return;listing=more?{...data,items:[...listing.items,...data.items]}:data;renderList();}catch(e){if(ticket===requestId)error(e);}}
function renderList(){screen(archived?'Уволенные сотрудники':'Сотрудники',`<div class="pb-people-actions">${archived?'': '<button class="pb-people-button primary" data-people="add">+ Добавить сотрудника</button>'}<button class="pb-people-button" data-people="${archived?'active':'archive'}">${archived?'← Действующие':'🗂 Уволенные'}</button><button class="pb-people-button" data-people="hotels">Отели</button><button class="pb-people-button" data-people="documents">Документы</button></div><label>Найти сотрудника<input id="pbPeopleSearch" type="search" placeholder="${standaloneMode()?'Имя или номер сотрудника':'Имя или Telegram ID'}"></label><p class="pb-people-muted">${archived?'Уволенные не показываются в общем рабочем списке. История, продажи и аудит сохраняются.':'В общем списке показываются только действующие сотрудники.'}</p><div id="pbPeopleRows"></div>${listing.next?'<button class="pb-people-button" data-people="more">Загрузить ещё</button>':''}`);renderRows();}
function renderRows(){const q=(dialog.querySelector('#pbPeopleSearch')?.value||'').toLowerCase();dialog.querySelector('#pbPeopleRows').innerHTML=listing.items.filter(x=>`${x.name} ${employeeIdentity(x)}`.toLowerCase().includes(q)).map(x=>{const owner=x.roles?.includes('OWNER');return `<article class="pb-people-row"><div><strong>${esc(x.name)}</strong><small>${esc(x.ownerAccessGranted?'Администратор · права владельца разрешены':labels(x.roles))}</small><small>ID ${esc(employeeIdentity(x))} · ${x.active?'Доступ активен':'Доступ отключён'}</small>${listing.canViewLastLogin?`<small>Последний вход: ${esc(formatLogin(x.lastLoginAt))}</small>`:''}<small>${owner?'Скриншоты владельцу разрешены всегда':(x.screenCaptureAllowed?'Скриншоты разрешены':'Скриншоты запрещены')}</small></div><div class="pb-people-actions">${x.qrManageable?`<button class="pb-people-button" data-people-qr="${x.id}">QR для входа</button>`:''}${listing.canManageOwnerAccess&&(x.ownerAccessGranted||(x.active&&x.roles.includes('ADMIN')&&!owner))?`<button class="pb-people-button" data-owner-toggle="${x.id}" data-owner-allowed="${x.ownerAccessGranted?'1':'0'}">${x.ownerAccessGranted?'Отозвать права владельца':'Разрешить права владельца'}</button>`:''}${listing.canManageScreenCapture&&!owner?`<button class="pb-people-button" data-screen-toggle="${x.id}" data-screen-allowed="${x.screenCaptureAllowed?'1':'0'}">${x.screenCaptureAllowed?'Запретить скриншоты':'Разрешить скриншоты'}</button>`:''}${x.editable?`<button class="pb-people-button" data-people-edit="${x.id}">Открыть</button>`:'<small>Защищённая запись</small>'}</div></article>`}).join('')||'<p>Сотрудники не найдены.</p>';}
function form(id){const employee=id?listing.items.find(x=>x.id===id):null;if(id&&(!employee||!employee.editable))return;const roleOptions=['PHOTOGRAPHER','MANAGER',...(listing.canAssignAdmin?['ADMIN']:[])];screen(employee?'Карточка сотрудника':'Добавить сотрудника',`<form id="pbEmployeeForm" data-id="${employee?.id||''}"><label>Имя<input name="name" maxlength="150" value="${esc(employee?.name||'')}" required autocomplete="off"></label>${identityField(employee)}<fieldset><legend>Роли</legend>${roleOptions.map(r=>`<label><input type="checkbox" name="role" value="${r}" ${employee?.roles.includes(r)?'checked':''}>${esc(ROLE_NAMES[r]||r)}</label>`).join('')}</fieldset><fieldset><legend>Отели</legend>${listing.hotels.map(h=>`<label><input type="checkbox" name="hotel" value="${h.id}" ${employee?.hotelIds.includes(h.id)?'checked':''}>${esc(h.name)}</label>`).join('')||'<p>Добавьте отель в разделе «Сотрудники → Отели».</p>'}</fieldset>${employee?.qrManageable?`<section class="pb-people-qr-card"><h3>Вход сотрудника</h3><p>Персональный QR заменяет ввод логина и пароля. Его можно открыть и отправить повторно.</p><button class="pb-people-button primary" type="button" data-people-qr="${employee.id}">Открыть QR для входа</button></section>`:''}${employee?`<div class="pb-people-actions"><button class="pb-people-button ${employee.active?'danger':''}" type="button" data-employee-action="${employee.active?'fire':'restore'}" data-employee-id="${employee.id}">${employee.active?'⛔ Уволить сотрудника':'♻️ Восстановить сотрудника'}</button></div>`:''}<div class="pb-people-notice">Увольнение отключает доступ и переносит сотрудника в раздел «Уволенные». История не удаляется.</div><p class="pb-people-error" data-form-error hidden role="alert"></p><div class="pb-people-actions"><button class="pb-people-button primary" type="submit">Сохранить</button><button class="pb-people-button" type="button" data-people="list">Отмена</button></div></form>`);}
function hotels(){screen('Отели',`<div class="stack">${listing.hotels.map(h=>`<p>${esc(h.name)}</p>`).join('')||'<p>Активных отелей пока нет.</p>'}</div><form id="pbHotelForm"><label>Название отеля<input name="name" maxlength="200" required autocomplete="off"></label><p class="pb-people-error" data-form-error hidden role="alert"></p><div class="pb-people-actions"><button class="pb-people-button primary" type="submit">Добавить отель</button><button class="pb-people-button" type="button" data-people="list">К сотрудникам</button></div></form>`);}
async function documents(){const ticket=++requestId;screen('Документы','<p role="status">Загружаем документы…</p>');try{const d=await api('/documents');if(ticket!==requestId)return;screen('Документы · проект',`<div class="pb-people-notice"><strong>Не подписано. Подписание отключено.</strong><p>Это проект для согласования, а не готовое оформление сотрудника.</p></div><article class="pb-people-rules">${esc(d.general)}</article><div class="pb-people-notice"><strong>Договор об оказании услуг</strong><p>${esc(d.servicesContract)}</p><strong>Персональные данные</strong><p>${esc(d.dataConsent)}</p></div><p class="pb-people-muted">Версия: ${esc(d.version)}. Документ не принят автоматически при открытии приложения.</p>${me?.permissions.manageSchedule?'<button class="pb-people-button" data-people="list">К сотрудникам</button>':''}`);}catch(e){if(ticket===requestId)error(e);}}
function qrStatus(message){const el=dialog.querySelector('[data-qr-status]');if(el)el.textContent=message;}
function validQr(data,id){
 if(!data?.enabled||data.employee?.id!==id||typeof data.image!=='string'||data.image.length>512000||!/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(data.image))throw new Error('Не удалось получить QR. Откройте карточку заново.');
 const url=new URL(data.url);
 if(url.protocol!=='https:'||url.username||url.password||url.pathname!=='/staff-login/'||url.search||!/^#token=[A-Za-z0-9._%-]+$/.test(url.hash)||url.href.length>2048)throw new Error('Ссылка входа недоступна. Откройте карточку заново.');
 return data;
}
function renderQr(data){
 currentQr=data;
 screen('QR для входа сотрудника',`<section class="pb-people-qr"><p class="pb-people-qr-label">Персональный вход</p><h3>${esc(data.employee.name)}</h3><p class="pb-people-muted">${esc(labels(data.employee.roles))}</p><figure class="pb-people-qr-image"><img src="${data.image}" alt="Персональный QR для входа: ${esc(data.employee.name)}" width="320" height="320"></figure><p>Сотрудник сканирует QR камерой телефона и подтверждает вход в свой аккаунт.</p><div class="pb-people-notice">Этот код заменяет логин и пароль. Отправляйте его только этому сотруднику. Тот же QR можно открыть здесь повторно в любое время.</div><div class="pb-people-actions pb-people-qr-primary"><button class="pb-people-button primary" type="button" data-qr-action="share">Поделиться QR</button><button class="pb-people-button" type="button" data-qr-action="download">Скачать PNG</button><button class="pb-people-button" type="button" data-qr-action="copy">Скопировать ссылку</button></div><p class="pb-people-muted" data-qr-status role="status" aria-live="polite"></p><div class="pb-people-actions"><button class="pb-people-button" type="button" data-qr-action="rotate">Заменить QR</button><button class="pb-people-button danger" type="button" data-qr-action="revoke">Отключить QR</button><button class="pb-people-button" type="button" data-people="list">К сотрудникам</button></div></section>`,true);
}
async function openQr(id,rotate=false){
 const employee=listing?.items.find(x=>x.id===id);
 if(!employee?.qrManageable)return;
 requestId++;
 screen('QR для входа сотрудника','<p role="status">Готовим персональный QR…</p>');
 const ticket=++qrRequestId,account=me?.user?.id;
 try{
  const data=await api(`/people/${id}/login-qr${rotate?'/rotate':''}`,{method:'POST',body:{}});
  if(ticket!==qrRequestId||!dialog.open||account!==me?.user?.id)return;
  renderQr(validQr(data,id));
 }catch(err){
  if(ticket!==qrRequestId||!dialog.open||account!==me?.user?.id)return;
  if([401,403].includes(err.status))return error(err);
  screen('Не удалось открыть QR',`<p role="alert">${esc(err.message)}</p><div class="pb-people-actions"><button class="pb-people-button primary" type="button" data-people-qr="${id}">Повторить</button><button class="pb-people-button" type="button" data-people="list">К сотрудникам</button></div>`);
 }
}
function qrFile(data){
 const bytes=Uint8Array.from(atob(data.image.slice(data.image.indexOf(',')+1)),c=>c.charCodeAt(0));
 const name=String(data.employee.name||'Сотрудник').replace(/[\\/:*?"<>|\u0000-\u001f]/g,'').trim().slice(0,80)||'Сотрудник';
 return new File([bytes],`Фотобосс — ${name} — вход.png`,{type:'image/png'});
}
function downloadQr(data){
 const file=qrFile(data),url=URL.createObjectURL(file),link=document.createElement('a');
 link.href=url;link.download=file.name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function qrAction(action,button){
 const data=currentQr;
 if(!data||!dialog.open)return;
 const id=data.employee.id;
 if(action==='download'){downloadQr(data);qrStatus('PNG сохранён. Его можно отправить сотруднику в любом мессенджере.');return;}
 if(action==='copy'){
  try{await navigator.clipboard.writeText(data.url);if(currentQr===data)qrStatus('Ссылка скопирована. Отправьте её этому сотруднику.');}
  catch{if(currentQr===data)qrStatus('Не удалось скопировать ссылку. Скачайте PNG и отправьте изображение.');}
  return;
 }
 if(action==='share'){
  const file=qrFile(data);
  if(!navigator.share||!navigator.canShare?.({files:[file]})){downloadQr(data);qrStatus('QR сохранён как PNG. Прикрепите его в сообщении сотруднику.');return;}
  try{await navigator.share({files:[file],title:`Фотобосс · вход ${data.employee.name}`,text:'Ваш персональный QR для входа в Фотобосс.'});if(currentQr===data)qrStatus('QR передан в выбранное приложение.');}
  catch(err){if(currentQr===data&&err.name!=='AbortError')qrStatus('Не удалось открыть отправку. Скачайте PNG и прикрепите его к сообщению.');}
  return;
 }
 if(action==='rotate'){
  if(confirm(`Заменить QR для ${data.employee.name}? Старый код перестанет работать. Сотруднику понадобится новый QR.`))await openQr(id,true);
  return;
 }
 if(action==='revoke'){
  if(!confirm(`Отключить QR для ${data.employee.name}? Вход по этому коду и созданные через него сеансы будут отключены.`))return;
  button.disabled=true;const ticket=qrRequestId,account=me?.user?.id;
  try{
   await api(`/people/${id}/login-qr`,{method:'DELETE',body:{}});
   if(ticket!==qrRequestId||!dialog.open||account!==me?.user?.id)return;
   screen('QR отключён',`<p>Код сотрудника ${esc(data.employee.name)} больше не даёт вход в приложение.</p><div class="pb-people-actions"><button class="pb-people-button primary" type="button" data-people-qr="${id}">Создать новый QR</button><button class="pb-people-button" type="button" data-people="list">К сотрудникам</button></div>`);
  }catch(err){if(ticket===qrRequestId&&dialog.open&&account===me?.user?.id)error(err);}
  finally{if(button.isConnected)button.disabled=false;}
 }
}
dialog.addEventListener('click',event=>{
 const open=event.target.closest('[data-people-qr]');
 if(open){event.preventDefault();openQr(Number(open.dataset.peopleQr));return;}
 const action=event.target.closest('[data-qr-action]');
 if(action){event.preventDefault();qrAction(action.dataset.qrAction,action).catch(()=>qrStatus('Не удалось подготовить QR. Откройте его заново.'));}
});
dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
dialog.addEventListener('input',e=>{if(e.target.id==='pbPeopleSearch')renderRows();});
dialog.addEventListener('click',async e=>{const ownerToggle=e.target.closest('[data-owner-toggle]');if(ownerToggle){const id=Number(ownerToggle.dataset.ownerToggle),allowed=ownerToggle.dataset.ownerAllowed==='1',employee=listing.items.find(x=>x.id===id);if(!employee)return;if(!confirm(`${allowed?'Отозвать':'Разрешить'} права владельца: ${employee.name}? ${allowed?'Останутся права администратора.':'Будут доступны функции владельца, кроме передачи этих прав другим сотрудникам.'}`))return;ownerToggle.disabled=true;try{await api(`/people/${id}/owner-access`,{method:'PUT',body:{allowed:!allowed}});await openList(false,archived);}catch(err){error(err);}finally{ownerToggle.disabled=false;}return;}const screenToggle=e.target.closest('[data-screen-toggle]');if(screenToggle){const id=Number(screenToggle.dataset.screenToggle),allowed=screenToggle.dataset.screenAllowed==='1';screenToggle.disabled=true;try{await api(`/people/${id}/screen-capture`,{method:'PUT',body:{allowed:!allowed}});await openList(false,archived);}catch(err){error(err);}return;}const edit=e.target.closest('[data-people-edit]');if(edit)return form(Number(edit.dataset.peopleEdit));const employeeAction=e.target.closest('[data-employee-action]');if(employeeAction){const id=Number(employeeAction.dataset.employeeId),action=employeeAction.dataset.employeeAction,employee=listing.items.find(x=>x.id===id);if(!employee)return;if(action==='fire'&&!confirm(`Уволить ${employee.name}? Доступ будет отключён, история сохранится.`))return;if(action==='restore'&&!confirm(`Восстановить ${employee.name}?`))return;employeeAction.disabled=true;try{await api(`/people/${id}/${action}`,{method:'POST',body:{}});await openList(false,action==='fire'?false:true);}catch(err){error(err);}return;}const action=e.target.closest('[data-people]')?.dataset.people;if(action==='close')close();if(action==='list')openList(false,archived);if(action==='more')openList(true,archived);if(action==='active')openList(false,false);if(action==='archive')openList(false,true);if(action==='add')form();if(action==='documents')documents();if(action==='hotels')hotels();});
dialog.addEventListener('submit',async e=>{if(e.target.id!=='pbHotelForm')return;e.preventDefault();const form=e.target,button=form.querySelector('[type=submit]');button.disabled=true;try{const result=await api('/hotels',{method:'POST',body:{name:new FormData(form).get('name')}});listing.hotels.push(result.hotel);hotels();}catch(err){error(err);}finally{button.disabled=false;}});
dialog.addEventListener('submit',async e=>{if(e.target.id!=='pbEmployeeForm')return;e.preventDefault();const f=e.target,b=f.querySelector('[type=submit]'),data=new FormData(f),id=Number(f.dataset.id)||null;const body={name:data.get('name'),roles:data.getAll('role'),hotelIds:data.getAll('hotel').map(Number)};if(!body.roles.length)return error(new Error('Выберите хотя бы одну роль.'));if(id){const old=listing.items.find(x=>x.id===id);body.active=old.active;body.revision=old.revision;}else if(!standaloneMode())body.telegramId=data.get('telegramId');b.disabled=true;try{await api('/people'+(id?`/${id}`:''),{method:id?'PUT':'POST',body});await openList();}catch(err){error(err);}finally{if(b.isConnected)b.disabled=false;}});
function inject(){if(!me)return;const top=document.querySelector('#topbar');if(!top?.children.length||top.querySelector('[data-open-people]'))return;const button=document.createElement('button');button.className='pb-people-trigger';button.dataset.openPeople='1';const editor=me.user.roles.some(r=>['OWNER','ADMIN'].includes(r));button.innerHTML=icon(editor?'users':'file')+'<span class="pb-people-trigger-label">'+(editor?'Сотрудники':'Документы')+'</span>';button.setAttribute('aria-label',editor?'Сотрудники и документы':'Документы приложения');button.addEventListener('click',()=>editor?openList():documents());(top.querySelector('.topbar-actions')||top).append(button);}
async function boot(){try{me=await api('/me');inject();}catch{me=null;}}
const observer=new MutationObserver(()=>{if(me)inject();else if(document.querySelector('#topbar')?.children.length)boot();});observer.observe(document.querySelector('#topbar'),{childList:true});boot();
document.addEventListener('pb-chat-account-reset',()=>{close();me=null;listing=null;});
document.addEventListener('pb-chat-account-ready',event=>{const next=event.detail;if(!next?.user)return;if(me?.user?.id!==next.user.id){close();listing=null;}me=next;inject();});
window.addEventListener('storage',event=>{if(event.key==='pb-account-changed'){close();me=null;listing=null;}});
window.addEventListener('pagehide',()=>{clearQr();dialog.close();dialog.replaceChildren();});
