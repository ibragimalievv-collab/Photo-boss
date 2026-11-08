import {api} from './api.js';
import {esc} from './domain.js';

let actor=null,generation=0;
const choices=new Map(),urls=new Set(),dialogs=new Set();
function current(panel,ticket){return ticket===generation&&panel.isConnected;}
function reset(){generation++;choices.clear();for(const dialog of dialogs)dialog.remove();dialogs.clear();for(const url of urls)URL.revokeObjectURL(url);urls.clear();}
function state(panel){const id=Number(panel.dataset.booking);if(!choices.has(id))choices.set(id,{ids:new Set(),loaded:false,available:new Set()});return choices.get(id);}
export function gallerySaleView(booking,data){
 if(actor!==data.actorId){reset();actor=data.actorId;}
 setTimeout(()=>{const panel=document.querySelector(`[data-sale-gallery][data-booking="${booking.id}"]`),form=panel?.closest('form');if(form?.elements.photoSource?.value==='gallery')void openGallerySale(form).catch(error=>{if(panel.isConnected)panel.querySelector('[data-gallery-sale-message]').textContent=error.message;});},0);
 return `<section data-sale-gallery data-booking="${booking.id}"><label class="field"><input type="radio" name="photoSource" value="gallery"> Выбрать из загруженной съёмки в галерее</label><div data-gallery-sale-body hidden><p>Отметьте купленные кадры. Они будут использованы в этой продаже без повторной загрузки.</p><button type="button" class="btn ghost" data-gallery-sale-refresh>Обновить фотографии</button><div data-gallery-sale-photos></div><p data-gallery-sale-message role="status"></p></div></section>`;
}
export function galleryRecoveryKeys(rows,keys){
 let current=[...keys];const visited=new Set();
 for(let count=0;count<16;count++){
  const previous=current.map(key=>rows.find(row=>row.key===key));
  const replacement=previous.find(row=>row?.supersededByGallery)?.supersededByGallery;
  if(!replacement||visited.has(replacement))return current;
  visited.add(replacement);
  const complete=rows.find(row=>row.key===replacement&&row.kind==='sale_complete');
  if(!complete?.recoveryOf)return current;
  const added=rows.filter(row=>row.recoveryOf===complete.recoveryOf);
  current=[...previous.filter(row=>row?.status==='synced'&&row.kind!=='sale_complete').map(row=>row.key),...added.map(row=>row.key)];
 }
 return current;
}
export function selectedGalleryPhotos(form){
 if(form.elements.photoSource?.value!=='gallery')return [];
 const panel=form.querySelector('[data-sale-gallery]'),saved=panel&&state(panel);
 if(!saved?.loaded)throw Error('Откройте загруженные фотографии и отметьте купленные кадры.');
 return [...saved.ids].filter(id=>saved.available.has(id)).map(photo=>({photo}));
}
export async function openGallerySale(form){
 const panel=form.querySelector('[data-sale-gallery]');if(!panel)throw Error('Для нового гостя сначала создайте именную съёмку.');
 const saved=state(panel),ticket=generation,body=panel.querySelector('[data-gallery-sale-body]');
 body.hidden=false;
 const input=form.querySelector('input[name=selected]');if(input)input.closest('label').hidden=true;
 const disk=form.querySelector('[data-disk-source]');if(disk)disk.hidden=true;
 const message=panel.querySelector('[data-gallery-sale-message]');message.textContent='Проверяем загруженную съёмку…';
 await api(`/bookings/${panel.dataset.booking}/gallery-originals/import`,{method:'POST',body:{},allowSnapshot:false});
 if(!current(panel,ticket))return;
 const result=await api(`/bookings/${panel.dataset.booking}/gallery-originals`,{allowSnapshot:false});
 if(!current(panel,ticket))return;
 saved.available=new Set(result.photos.filter(p=>!p.accepted).map(p=>p.id));
 for(const id of saved.ids)if(!saved.available.has(id))saved.ids.delete(id);
 saved.loaded=true;
 panel.querySelector('[data-gallery-sale-photos]').innerHTML=result.photos.map(p=>`<label class="field"><input type="checkbox" data-gallery-sale-photo="${p.id}" ${p.accepted?'disabled':saved.ids.has(p.id)?'checked':''}><span>${esc(p.name)} · ${Math.ceil(p.bytes/1024)} КБ${p.accepted?' · уже принято в эту продажу':''}</span><button type="button" class="btn ghost" data-gallery-sale-preview="${p.id}">Посмотреть</button></label>`).join('')||'<p>Полные оригиналы пока не загружены. Откройте «Диск и клиентские галереи» и добавьте съёмку гостя.</p>';
 message.textContent=`Отмечено новых кадров: ${saved.ids.size}. Оплата и доступ гостю проверяются отдельно.`;
 form.dispatchEvent(new CustomEvent('pb-gallery-selection',{bubbles:true}));
}
document.addEventListener('change',event=>{
 const form=event.target.closest?.('#workflowSale');if(!form)return;
 const panel=form.querySelector('[data-sale-gallery]');if(!panel)return;
 if(event.target.matches('[data-gallery-sale-photo]')){
  const saved=state(panel),id=Number(event.target.dataset.gallerySalePhoto);if(event.target.checked&&saved.available.has(id))saved.ids.add(id);else saved.ids.delete(id);
  panel.querySelector('[data-gallery-sale-message]').textContent=`Отмечено новых кадров: ${saved.ids.size}.`;
  form.dispatchEvent(new CustomEvent('pb-gallery-selection',{bubbles:true}));return;
 }
 if(event.target.name!=='photoSource')return;
 const input=form.querySelector('input[name=selected]');if(input)input.closest('label').hidden=event.target.value!=='device';
 const disk=form.querySelector('[data-disk-source]');if(disk&&['device','gallery'].includes(event.target.value))disk.hidden=true;
 panel.querySelector('[data-gallery-sale-body]').hidden=event.target.value!=='gallery';
 if(event.target.value==='gallery')void openGallerySale(form).catch(error=>{if(panel.isConnected)panel.querySelector('[data-gallery-sale-message]').textContent=error.message;});
});
document.addEventListener('click',async event=>{
 const button=event.target.closest?.('[data-gallery-sale-refresh],[data-gallery-sale-preview]'),form=button?.closest('#workflowSale');if(!form)return;
 const panel=form.querySelector('[data-sale-gallery]'),ticket=generation;button.disabled=true;
 try{
  if(button.hasAttribute('data-gallery-sale-refresh'))await openGallerySale(form);
  else{
   const blob=await api(`/bookings/${panel.dataset.booking}/gallery-originals/${Number(button.dataset.gallerySalePreview)}/preview`,{responseType:'blob',allowSnapshot:false});
   if(!current(panel,ticket))return;
   const dialog=document.createElement('dialog'),image=document.createElement('img'),close=document.createElement('button'),url=URL.createObjectURL(blob);urls.add(url);
   image.src=url;image.alt='Оригинал загруженной съёмки';image.style.maxWidth='100%';close.type='button';close.className='btn';close.textContent='Закрыть';close.onclick=()=>dialog.close();
   dialog.append(close,image);dialogs.add(dialog);dialog.addEventListener('close',()=>{URL.revokeObjectURL(url);urls.delete(url);dialogs.delete(dialog);dialog.remove();},{once:true});document.body.append(dialog);dialog.showModal();
  }
 }catch(error){if(current(panel,ticket))panel.querySelector('[data-gallery-sale-message]').textContent=error.message;}finally{if(button.isConnected)button.disabled=false;}
});
window.addEventListener('hashchange',reset);window.addEventListener('pagehide',reset);document.addEventListener('pb-chat-account-reset',reset);
