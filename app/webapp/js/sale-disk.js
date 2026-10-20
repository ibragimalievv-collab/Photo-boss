import {api} from './api.js';
import {esc} from './domain.js';
let actor=null,version=0;
const selections=new Map();
function state(panel){const key=Number(panel.dataset.booking);if(!selections.has(key))selections.set(key,{paths:new Set(),revision:null});return selections.get(key);}
export function saleDiskView(booking,data){
 if(actor!==data.actorId){actor=data.actorId;version++;selections.clear();}
 setTimeout(()=>{
  const panel=document.querySelector(`[data-sale-disk][data-booking="${booking.id}"]`),form=panel?.closest('form');
  if(!form||(form.elements.photoSource?.value||'device')==='device')return;
  panel.querySelector('[data-disk-source]').hidden=false;
  const upload=form.querySelector('input[name=selected]');if(upload)upload.closest('label').hidden=true;
  const ticket=++version;void linked(panel,ticket).catch(error=>{if(active(panel,ticket))note(panel,error.message);});
 },0);
 return `<section data-sale-disk data-booking="${booking.id}" data-owner="${data.canAuditDisk?'1':'0'}"><fieldset><legend>Откуда добавить выбранные фотографии</legend><label><input type="radio" name="photoSource" value="device" checked> Загрузить с устройства</label><label><input type="radio" name="photoSource" value="link"> Прикрепить ссылку</label><label><input type="radio" name="photoSource" value="disk"> Выбрать на Яндекс.Диске</label></fieldset><div data-disk-source hidden><label class="field">Ссылка Яндекс.Диска или путь папки<input class="input" name="diskSource" maxlength="1500" placeholder="https://disk.yandex.ru/d/… или disk:/Съёмки/…"></label><button type="button" class="btn ghost" data-sale-disk-action="connect">Прикрепить ссылку / папку</button>${data.canAuditDisk?'<button type="button" class="btn ghost" data-sale-disk-action="browse" data-path="disk:/">Выбрать папку рабочего Диска</button>':''}<button type="button" class="btn ghost" data-sale-disk-action="linked">Открыть привязанную папку</button><p>Закрытую папку рабочего Диска привязывает владелец. Сотрудник видит только папку своей продажи. Ссылку с доступными фотографиями можно прикрепить к своей съёмке. Приложение само не публикует файлы и не загружает копии.</p><div data-sale-disk-folders></div><div data-sale-disk-files></div><p data-sale-disk-selection></p><p data-sale-disk-message role="status"></p></div></section>`;
}
export function selectedDiskPhotos(form){
 if((form.elements.photoSource?.value||'device')==='device')return [];
 const panel=form.querySelector('[data-sale-disk]'),s=panel&&state(panel);
 if(!s?.revision)throw new Error('Сначала прикрепите ссылку или откройте папку Яндекс.Диска.');
 return [...s.paths].map(path=>({path,revision:s.revision}));
}
function note(panel,text){const el=panel.querySelector('[data-sale-disk-message]');if(el)el.textContent=text;}
function active(panel,ticket){return panel.isConnected&&ticket===version;}
function selection(panel){panel.querySelector('[data-sale-disk-selection]').textContent=`Выбрано на Диске: ${state(panel).paths.size}. Отметьте только купленные кадры; повторные файлы сервер не посчитает дважды.`;}
async function files(panel,next,ticket){
 const id=panel.dataset.booking,result=await api(`/bookings/${id}/disk-files`+(next?'?path='+encodeURIComponent(next):''),{allowSnapshot:false});
 if(!active(panel,ticket))return;
 const s=state(panel);if(s.revision!==result.revision){s.paths.clear();s.revision=result.revision;}
 const parent=result.path===result.root?null:result.path.slice(0,result.path.lastIndexOf('/'))||(result.root.startsWith('disk:')?'disk:/':'/');
 panel.querySelector('[data-sale-disk-files]').innerHTML=`<p>Папка: ${esc(result.path)}</p>${parent?`<button type="button" class="btn ghost" data-sale-disk-action="files" data-path="${esc(parent)}">На уровень выше</button>`:''}${result.items.map(f=>f.type==='dir'?`<button type="button" class="btn ghost" data-sale-disk-action="files" data-path="${esc(f.path)}">📁 ${esc(f.name)}</button>`:`<label class="field"><input type="checkbox" data-disk-photo="${esc(f.path)}" ${s.paths.has(f.path)?'checked':''}> ${esc(f.name)} · ${Math.ceil((f.size||0)/1024)} КБ</label>`).join('')||'<p>В этой папке нет фотографий JPEG, PNG или WebP. Откройте вложенную папку.</p>'}`;
 selection(panel);note(panel,'Фотографии читаются прямо с Яндекс.Диска. Выберите кадры и сохраните продажу обычной кнопкой ниже.');
}
async function linked(panel,ticket){
 const result=await api(`/bookings/${panel.dataset.booking}/disk-folder`,{allowSnapshot:false});
 if(!active(panel,ticket))return;
 if(!result.connected){note(panel,'Папка ещё не привязана. Прикрепите ссылку или выберите папку рабочего Диска.');return;}
 panel.querySelector('[name=diskSource]').value=result.source;
 await files(panel,null,ticket);
}
async function browse(panel,path,ticket){
 const result=await api('/storage/disk-folders?path='+encodeURIComponent(path),{allowSnapshot:false});
 if(!active(panel,ticket))return;
 const parent=path==='disk:/'?null:path.slice(0,path.lastIndexOf('/'))||'disk:';
 panel.querySelector('[data-sale-disk-folders]').innerHTML=`<p>Рабочий Диск: ${esc(path)}</p>${parent?`<button type="button" class="btn ghost" data-sale-disk-action="browse" data-path="${esc(parent==='disk:'?'disk:/':parent)}">На уровень выше</button>`:''}${result.folders.map(f=>`<button type="button" class="btn ghost" data-sale-disk-action="browse" data-path="${esc(f.path)}">📁 ${esc(f.name)}</button>`).join('')}<button type="button" class="btn primary" data-sale-disk-action="bind" data-path="${esc(path)}">Привязать эту папку к продаже</button>`;
}
document.addEventListener('change',async event=>{
 const panel=event.target.closest?.('[data-sale-disk]');if(!panel)return;
 if(event.target.matches('[data-disk-photo]')){const s=state(panel),path=event.target.dataset.diskPhoto;if(event.target.checked)s.paths.add(path);else s.paths.delete(path);selection(panel);return;}
 if(event.target.name!=='photoSource')return;
 version++;const ticket=version,device=event.target.value==='device';panel.querySelector('[data-disk-source]').hidden=device;
 const upload=panel.closest('form').querySelector('input[name=selected]');if(upload)upload.closest('label').hidden=!device;
 if(!device)try{await linked(panel,ticket);}catch(error){if(active(panel,ticket))note(panel,error.message);}
});
document.addEventListener('click',async event=>{
 const button=event.target.closest?.('[data-sale-disk-action]'),panel=button?.closest('[data-sale-disk]');if(!panel)return;
 const ticket=++version;button.disabled=true;
 try{
  const action=button.dataset.saleDiskAction;
  if(action==='browse')await browse(panel,button.dataset.path,ticket);
  else if(action==='files')await files(panel,button.dataset.path,ticket);
  else if(action==='linked')await linked(panel,ticket);
  else{
   const source=action==='bind'?button.dataset.path:panel.querySelector('[name=diskSource]').value;
   await api(`/bookings/${panel.dataset.booking}/disk-folder`,{method:'POST',body:{source}});
   if(active(panel,ticket)){panel.querySelector('[data-sale-disk-folders]').innerHTML='';await linked(panel,ticket);}
  }
 }catch(error){if(active(panel,ticket))note(panel,error.message);}finally{if(button.isConnected)button.disabled=false;}
});
window.addEventListener('hashchange',()=>{version++;});
window.addEventListener('pagehide',()=>{version++;});
