import {api} from './api.js';
import {esc} from './domain.js';
const DEVICE_DISK_URL='https://disk.yandex.ru/client/disk';
let actor=null,version=0;
const selections=new Map();
function state(panel){const key=Number(panel.dataset.booking);if(!selections.has(key))selections.set(key,{paths:new Set(),revision:null});return selections.get(key);}
export function saleDiskView(booking,data){
 if(actor!==data.actorId){actor=data.actorId;version++;selections.clear();}
 setTimeout(()=>{
  const panel=document.querySelector(`[data-sale-disk][data-booking="${booking.id}"]`),form=panel?.closest('form');
  if(!form||(form.elements.photoSource?.value||'device')==='device')return;
  showSource(panel);
  const ticket=++version;void openSource(panel,ticket).catch(error=>failure(panel,error,ticket));
 },0);
 return `<section data-sale-disk data-booking="${booking.id}" data-owner="${data.canAuditDisk?'1':'0'}"><fieldset><legend>Откуда добавить выбранные фотографии</legend><label><input type="radio" name="photoSource" value="device" checked> Загрузить с устройства</label><label><input type="radio" name="photoSource" value="link"> Прикрепить ссылку</label><label><input type="radio" name="photoSource" value="disk"> Выбрать на Яндекс.Диске</label></fieldset><p>При выборе Яндекс.Диска откроется отдельное окно Диска на вашем устройстве.</p><div data-disk-source hidden><div data-disk-external hidden><a class="btn primary" href="${DEVICE_DISK_URL}" target="_blank" rel="noopener noreferrer" data-open-device-disk>Открыть Яндекс.Диск</a><p>Если отдельное окно не открылось, нажмите «Открыть Яндекс.Диск». В Диске выберите нужные фотографии или папку → «Поделиться» → «Скопировать ссылку». Вернитесь в Photo Boss и вставьте ссылку ниже. Используется аккаунт Яндекса на вашем устройстве.</p><p>Можно также выбрать фотографии из подключённой папки прямо здесь. Черновик продажи сохраняется в этом окне.</p></div><div data-disk-link hidden><label class="field">Ссылка Яндекс.Диска или путь папки<input class="input" name="diskSource" maxlength="1500" placeholder="https://disk.yandex.ru/d/… или disk:/Съёмки/…"></label><button type="button" class="btn ghost" data-sale-disk-action="connect">Прикрепить ссылку / папку</button></div>${data.canAuditDisk?'<button type="button" class="btn ghost" data-sale-disk-action="browse" data-disk-root data-path="disk:/">Выбрать папку рабочего Диска</button>':''}<button type="button" class="btn ghost" data-sale-disk-action="linked">Открыть привязанную папку</button><p>Выберите папку и отметьте купленные фотографии. Закрытую папку рабочего Диска привязывает владелец. Сотрудник видит только папку своей продажи.</p><div data-sale-disk-folders></div><div data-sale-disk-files></div><p data-sale-disk-selection></p><p data-sale-disk-message role="status"></p><button type="button" class="btn ghost" data-sale-disk-action="retry" hidden>Повторить открытие</button></div></section>`;
}
export function selectedDiskPhotos(form){
 if((form.elements.photoSource?.value||'device')==='device')return [];
 const panel=form.querySelector('[data-sale-disk]'),s=panel&&state(panel);
 if(!s?.revision)throw new Error('Сначала прикрепите ссылку или откройте папку Яндекс.Диска.');
 return [...s.paths].map(path=>({path,revision:s.revision}));
}
function note(panel,text){const el=panel.querySelector('[data-sale-disk-message]');if(el)el.textContent=text;}
function active(panel,ticket){return panel.isConnected&&ticket===version;}
function openDeviceDisk(){
 const tg=window.Telegram?.WebApp;
 if(tg?.initData&&typeof tg.openLink==='function')try{tg.openLink(DEVICE_DISK_URL);return;}catch{}
 try{window.open(DEVICE_DISK_URL,'_blank','noopener,noreferrer');}catch{/* The visible link remains available. */}
}
function showSource(panel){
 const form=panel.closest('form'),mode=form.elements.photoSource?.value||'device';
 panel.querySelector('[data-disk-source]').hidden=mode==='device';
 panel.querySelector('[data-disk-link]').hidden=mode==='device';
 panel.querySelector('[data-disk-external]').hidden=mode!=='disk';
 const root=panel.querySelector('[data-disk-root]');if(root)root.hidden=mode!=='disk';
 const upload=form.querySelector('input[name=selected]');if(upload)upload.closest('label').hidden=mode!=='device';
}
function loading(panel,action,path=''){
 panel.dataset.retryAction=action;panel.dataset.retryPath=path;
 panel.querySelector('[data-sale-disk-action="retry"]').hidden=true;
 note(panel,'Открываем Яндекс.Диск…');
}
function failure(panel,error,ticket){
 if(!active(panel,ticket))return;
 note(panel,error.message||'Не удалось открыть Яндекс.Диск. Проверьте соединение и повторите.');
 panel.querySelector('[data-sale-disk-action="retry"]').hidden=false;
}
async function openSource(panel,ticket){
 panel.querySelector('[data-sale-disk-folders]').innerHTML='';
 panel.querySelector('[data-sale-disk-action="retry"]').hidden=true;
 if(panel.closest('form').elements.photoSource?.value==='link'){
  note(panel,'Вставьте ссылку и нажмите «Прикрепить ссылку / папку».');return;
 }
 await linked(panel,ticket);
}
function selection(panel){panel.querySelector('[data-sale-disk-selection]').textContent=`Выбрано на Диске: ${state(panel).paths.size}. Отметьте только купленные кадры; повторные файлы сервер не посчитает дважды.`;}
async function files(panel,next,ticket){
 loading(panel,'files',next||'');
 const id=panel.dataset.booking,result=await api(`/bookings/${id}/disk-files`+(next?'?path='+encodeURIComponent(next):''),{allowSnapshot:false});
 if(!active(panel,ticket))return;
 const s=state(panel);if(s.revision!==result.revision){s.paths.clear();s.revision=result.revision;}
 const parent=result.path===result.root?null:result.path.slice(0,result.path.lastIndexOf('/'))||(result.root.startsWith('disk:')?'disk:/':'/');
 panel.querySelector('[data-sale-disk-files]').innerHTML=`<p>Папка: ${esc(result.path)}</p>${parent?`<button type="button" class="btn ghost" data-sale-disk-action="files" data-path="${esc(parent)}">На уровень выше</button>`:''}${result.items.map(f=>f.type==='dir'?`<button type="button" class="btn ghost" data-sale-disk-action="files" data-path="${esc(f.path)}">📁 ${esc(f.name)}</button>`:`<label class="field"><input type="checkbox" data-disk-photo="${esc(f.path)}" ${s.paths.has(f.path)?'checked':''}> ${esc(f.name)} · ${Math.ceil((f.size||0)/1024)} КБ</label>`).join('')||'<p>В этой папке нет фотографий JPEG, PNG или WebP. Откройте вложенную папку.</p>'}`;
 selection(panel);note(panel,'Фотографии читаются прямо с Яндекс.Диска. Выберите кадры и сохраните продажу обычной кнопкой ниже.');
}
async function linked(panel,ticket){
 loading(panel,'linked');
 const result=await api(`/bookings/${panel.dataset.booking}/disk-folder`,{allowSnapshot:false});
 if(!active(panel,ticket))return;
 if(!result.connected){
  if(panel.dataset.owner==='1'&&panel.closest('form').elements.photoSource?.value==='disk')await browse(panel,'disk:/',ticket);
  else note(panel,'Владелец ещё не выбрал папку этой съёмки. Можно прикрепить доступную ссылку в пункте «Прикрепить ссылку».');
  return;
 }
 const source=panel.querySelector('[name=diskSource]');if(!source.value)source.value=result.source;
 panel.querySelector('[data-sale-disk-folders]').innerHTML='';
 await files(panel,null,ticket);
}
async function browse(panel,path,ticket){
 loading(panel,'browse',path);
 const result=await api('/storage/disk-folders?path='+encodeURIComponent(path),{allowSnapshot:false});
 if(!active(panel,ticket))return;
 const parent=path==='disk:/'?null:path.slice(0,path.lastIndexOf('/'))||'disk:';
 panel.querySelector('[data-sale-disk-folders]').innerHTML=`<p>Рабочий Диск: ${esc(path)}</p>${parent?`<button type="button" class="btn ghost" data-sale-disk-action="browse" data-path="${esc(parent==='disk:'?'disk:/':parent)}">На уровень выше</button>`:''}${result.folders.map(f=>`<button type="button" class="btn ghost" data-sale-disk-action="browse" data-path="${esc(f.path)}">📁 ${esc(f.name)}</button>`).join('')}<button type="button" class="btn primary" data-sale-disk-action="bind" data-path="${esc(path)}">Привязать эту папку к продаже</button>`;
 panel.querySelector('[data-sale-disk-files]').innerHTML='';
 note(panel,result.folders.length?'Откройте папку съёмки и нажмите «Привязать эту папку к продаже».':'Вложенных папок нет. Нажмите «Привязать эту папку к продаже», чтобы выбрать фотографии.');
}
document.addEventListener('change',async event=>{
 const panel=event.target.closest?.('[data-sale-disk]');if(!panel)return;
 if(event.target.matches('[data-disk-photo]')){const s=state(panel),path=event.target.dataset.diskPhoto;if(event.target.checked)s.paths.add(path);else s.paths.delete(path);selection(panel);return;}
 if(event.target.name!=='photoSource')return;
 // Open before any await, only for an actual user choice, never on form recovery.
 if(event.target.value==='disk'&&event.isTrusted)openDeviceDisk();
 const ticket=++version;showSource(panel);
 if(event.target.value!=='device')try{await openSource(panel,ticket);}catch(error){failure(panel,error,ticket);}
});
document.addEventListener('click',async event=>{
 const external=event.target.closest?.('[data-open-device-disk]'),tg=window.Telegram?.WebApp;
 if(external&&event.isTrusted&&tg?.initData&&typeof tg.openLink==='function'&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey){try{tg.openLink(DEVICE_DISK_URL);event.preventDefault();}catch{}return;}
 const button=event.target.closest?.('[data-sale-disk-action]'),panel=button?.closest('[data-sale-disk]');if(!panel)return;
 const ticket=++version;button.disabled=true;
 try{
  const retry=button.dataset.saleDiskAction==='retry',action=retry?panel.dataset.retryAction:button.dataset.saleDiskAction,path=retry?panel.dataset.retryPath:button.dataset.path;
  if(action==='browse')await browse(panel,path,ticket);
  else if(action==='files')await files(panel,path||null,ticket);
  else if(action==='linked')await linked(panel,ticket);
  else{
   const source=action==='bind'?path:panel.querySelector('[name=diskSource]').value;
   loading(panel,action,source);
   await api(`/bookings/${panel.dataset.booking}/disk-folder`,{method:'POST',body:{source}});
   if(active(panel,ticket)){if(action==='bind')panel.querySelector('[name=diskSource]').value=source;panel.querySelector('[data-sale-disk-folders]').innerHTML='';await linked(panel,ticket);}
  }
 }catch(error){failure(panel,error,ticket);}finally{if(button.isConnected)button.disabled=false;}
});
window.addEventListener('hashchange',()=>{version++;});
window.addEventListener('pagehide',()=>{version++;});
