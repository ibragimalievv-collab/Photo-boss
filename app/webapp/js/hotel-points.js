import {api} from './api.js';
import {esc} from './domain.js';

export async function hotelPointsView(){
 const data=await api('/hotel-points',{allowSnapshot:false});
 return `<p>Начальная зона — круг вокруг метки. Радиус нужно сверить с фактической территорией отеля. GPS может быть подменён и не подтверждает человека или форму.</p><p>${data.reviewRequired?'Приход и уход подтверждает руководитель после проверки фото и обстоятельств.':'Ручная проверка прихода ещё не включена.'}</p>${data.items.map(h=>{
  const p=h.point;
  const field=(name,label,type,value,extra='')=>`<label class="field">${label}<input class="input" name="${name}" type="${type}" value="${esc(value??'')}" ${extra} required></label>`;
  const url=p?`https://yandex.ru/maps/?ll=${p.longitude}%2C${p.latitude}&z=17&pt=${p.longitude}%2C${p.latitude}%2Cpm2rdm`:'';
  return `<details class="panel section" ${p?'open':''}><summary data-hotel-name="${esc(h.name)}">${esc(h.name)} · ${p?'Зона задана':'Нет зоны'}</summary><p class="hotel-point-description" ${p?'':'hidden'}>${p?`${esc(p.address)} · радиус ${p.radiusM} м`:''}</p><a class="btn ghost hotel-point-map" href="${url}" target="_blank" rel="noopener noreferrer" ${p?'':'hidden'}>Посмотреть точку на карте</a><form class="hotel-point-form form-grid" data-hotel="${h.id}" data-revision="${h.revision}">${field('address','Адрес','text',p?.address||h.address,'maxlength="300"')}${field('latitude','Широта','number',p?.latitude,'step="any" min="-90" max="90"')}${field('longitude','Долгота','number',p?.longitude,'step="any" min="-180" max="180"')}${field('radiusM','Радиус зоны, м','number',p?.radiusM||200,'min="20" max="1000" step="1"')}<p class="hotel-point-result" role="status"></p><button class="btn primary" type="submit">Сохранить зону</button></form></details>`;
 }).join('')||'<p>Доступных отелей нет.</p>'}`;
}

document.addEventListener('submit',async event=>{
 const form=event.target;if(!form.classList.contains('hotel-point-form'))return;
 event.preventDefault();const button=form.querySelector('button'),note=form.querySelector('.hotel-point-result');button.disabled=true;
 try{
  const fields=Object.fromEntries(new FormData(form));
  const saved=await api('/hotel-points',{method:'PUT',body:{hotelId:Number(form.dataset.hotel),revision:form.dataset.revision,
   point:{address:fields.address,latitude:Number(fields.latitude),longitude:Number(fields.longitude),radiusM:Number(fields.radiusM)}}});
  form.dataset.revision=saved.revision;const panel=form.closest('details'),summary=panel.querySelector('summary'),description=panel.querySelector('.hotel-point-description'),map=panel.querySelector('.hotel-point-map');summary.textContent=summary.dataset.hotelName+' · Зона задана';description.hidden=false;description.textContent=`${fields.address} · радиус ${fields.radiusM} м`;map.hidden=false;map.href=`https://yandex.ru/maps/?ll=${Number(fields.longitude)}%2C${Number(fields.latitude)}&z=17&pt=${Number(fields.longitude)}%2C${Number(fields.latitude)}%2Cpm2rdm`;note.textContent='Зона сохранена. Новые отметки проверяются по этой версии.';
 }catch(error){note.textContent=error.message;}
 finally{button.disabled=false;}
});
