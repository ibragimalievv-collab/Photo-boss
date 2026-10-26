import {api} from './api.js';
import {esc} from './domain.js';
let state;

export function bookingPriceNote(data,hotelId){
 const hotel=(data.hotels||[]).find(h=>h.id===Number(hotelId));
 const rates=(data.tariffs||[]).filter(t=>t.hotelId===Number(hotelId)&&t.kind==='UNIT');
 const selected=rates.find(t=>t.id===hotel?.fixedUnitTariffId)||(rates.length===1?rates[0]:null);
 const price=hotel?.unitPrice??selected?.price;
 if(price!=null)return `Цена отеля: ${Number(price).toLocaleString('ru-RU')} ₽ за кадр. Сохраняется в записи и не меняется при продаже.`;
 const legacy=rates.length===3&&[300,400,500].every(price=>rates.some(t=>Number(t.price)===price&&t.name===`Поштучно — ${price} ₽/кадр`));
 if(rates.length>1&&!legacy)return 'Владелец или администратор должен закрепить одну цену кадра для этого отеля. Случайный тариф не применяется.';
 return 'Цена выбранного вида съёмки сохранится при создании записи. Владелец или администратор может закрепить цену за отелем.';
}
export function pricingView(data){
 state=data;if(!data.canConfigurePricing)return '';
 return `<details class="panel section" id="hotelPricingPanel"><summary>Фиксированные цены отелей, пакеты и альбомы</summary><p>У каждого отеля одна цена кадра. Задавать её может владелец или администратор своего отеля. Изменение применяется только к новым записям; согласованные цены прежних записей и продаж сохраняются. Пустое число кадров у пакета означает всю съёмку. Печатный альбом — отдельный товар, его цена не меняет число купленных фотографий.</p>
 <form id="hotelPricingForm" class="form-grid"><input type="hidden" name="replaces"><input type="hidden" name="originalKind"><label class="field">Отель<select class="input" name="hotelId">${data.hotels.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join('')}</select></label><label class="field">Название<input class="input" name="name" minlength="2" maxlength="150" required></label><label class="field">Тип<select class="input" name="kind"><option value="UNIT">Фиксированная цена одного кадра в отеле</option><option value="PACKAGE">Пакет за фиксированную сумму</option><option value="ALBUM">Печатный альбом — цена за штуку</option></select></label><label class="field">Цена, ₽<input class="input" name="price" type="number" min="0.01" step="0.01" required></label><label class="field">Кадров в пакете (пусто — вся съёмка)<input class="input" name="photoCount" type="number" min="1" max="10000"></label><button class="btn primary">Сохранить тариф</button><p role="status"></p></form>
 <div id="hotelPricingRows">${pricingRows(data)}</div></details>`;
}
function pricingRows(data){return ` ${data.hotels.map(h=>`<p>${esc(h.name)} · ${esc(bookingPriceNote(data,h.id))}</p>`).join('')}
 ${(data.tariffs||[]).map(t=>`<p>${esc(data.hotels.find(h=>h.id===t.hotelId)?.name)} · ${esc(t.name)} · ${esc(t.price)} ₽ ${t.kind==='UNIT'?'за кадр':`за ${t.photoCount||'все'} фото`} <button type="button" class="btn ghost" data-tariff-edit="${t.id}">Изменить</button></p>`).join('')}
 ${(data.albums||[]).map(t=>`<p>${esc(data.hotels.find(h=>h.id===t.hotelId)?.name)} · ${esc(t.name)} · ${esc(t.price)} ₽/шт. <button type="button" class="btn ghost" data-album-edit="${t.id}">Изменить альбом</button></p>`).join('')}`;
}

function bookingUnit(b){
 return b.pricing||(Number(b.price)>0?{tariffId:null,kind:'UNIT',price:b.price,name:'Цена записи',photoCount:null}:null);
}
export function tariffOptions(b,tariffs){
 const fixed=b.draft?.pricing,unit=bookingUnit(b),packages=(tariffs||[]).filter(t=>t.hotelId===b.hotelId&&t.kind==='PACKAGE');
 const options=fixed?`<option value="${fixed.tariffId||0}" selected>Тариф сохранён · ${esc(fixed.kind==='PACKAGE'?fixed.name:`${fixed.price} ₽/кадр`)}</option>`:
 `${unit?`<option value="${unit.tariffId||0}" selected>Поштучно · ${esc(unit.price)} ₽ за кадр · цена записи</option>`:'<option value="" selected disabled>Цена записи требует проверки владельцем</option>'}${packages.map(t=>`<option value="${t.id}">Пакет «${esc(t.name)}» · ${esc(t.price)} ₽ за ${t.photoCount||'все'} фото</option>`).join('')}`;
 const chosenAlbum=(fixed?.lines||[]).find(line=>line.type==='ALBUM'),albums=(state?.albums||[]).filter(album=>album.hotelId===b.hotelId);
 const albumOptions=chosenAlbum?`<option value="${chosenAlbum.albumId}" selected>${esc(chosenAlbum.name)} · ${esc(chosenAlbum.price)} ₽/шт.</option>`:
 `<option value="">Без альбома</option>${albums.map(album=>`<option value="${album.id}">${esc(album.name)} · ${esc(album.price)} ₽/шт.</option>`).join('')}`;
 return `<label class="field">Способ расчёта<select class="input" name="tariffId" ${fixed?'disabled':'required'}>${options}</select></label><label class="field">Печатный альбом — отдельный товар<select class="input" name="albumId" ${fixed?'disabled':''}>${albumOptions}</select></label><label class="field">Количество альбомов<input class="input" name="albumQuantity" type="number" min="1" max="100" step="1" value="${chosenAlbum?.quantity||1}" ${fixed?'disabled':''}></label><div class="notice" id="salePricePreview" role="status">Укажите количество: приложение покажет сумму продажи, поступления и доплату.</div>`;
}
export function updatePricePreview(form,b,tariffs){
 if(!form||!b)return;
 const fixed=b.draft?.pricing,unit=bookingUnit(b),key=Number(form.elements.tariffId?.value);
 const t=fixed||(unit&&key===(unit.tariffId||0)?unit:(tariffs||[]).find(t=>t.id===key&&t.hotelId===b.hotelId&&t.kind==='PACKAGE'));
 const sold=Number(form.elements.sold?.value||0),price=Number(t?.price||0);
 let photoAmount=t?.kind==='PACKAGE'?price:t?.kind==='UNIT'&&Number.isInteger(sold)&&sold>0?Math.round(sold*price*100)/100:null;
 const discountId=fixed?.discountId||Number(form.elements.discountId?.value)||null,discounted=Boolean(discountId);
 const approval=(b.discountApprovals||[]).find(row=>row.id===discountId),total=Number(form.elements.total?.value);
 const validDiscount=!discounted||(approval?.status==='APPROVED'&&t?.kind==='UNIT'&&sold===total&&total===approval.total&&price===Number(approval.unitPrice)&&!Number(b.soldTotal||0));
 if(!validDiscount)photoAmount=null;
 if(discounted&&photoAmount!==null)photoAmount=Math.round(photoAmount*50)/100;
 const savedAlbum=(fixed?.lines||[]).find(line=>line.type==='ALBUM'),albumId=Number(form.elements.albumId?.value),albumQuantity=Number(form.elements.albumQuantity?.value||1);
 const album=savedAlbum||(state?.albums||[]).find(row=>row.id===albumId&&row.hotelId===b.hotelId);
 const albumCount=savedAlbum?.quantity??albumQuantity,albumAmount=album&&Number.isInteger(albumCount)&&albumCount>0&&albumCount<=100?Math.round(Number(album.price)*albumCount*100)/100:0;
 const amount=photoAmount===null||(albumId&&(!album||!Number.isInteger(albumCount)||albumCount<1||albumCount>100))?null:Math.round((photoAmount+albumAmount)*100)/100;
 const paid=Number(b.paidAmount||0),due=amount===null?null:Math.max(0,Math.round((Number(b.salesAmount||0)+amount-paid)*100)/100);
 const creditAmount=amount===null?null:Math.max(0,Math.round((paid-Number(b.salesAmount||0)-amount)*100)/100);
 const box=form.querySelector('#salePricePreview');
 if(box)box.textContent=amount===null?'Проверьте цену записи, способ расчёта и количество кадров.':`${t.kind==='PACKAGE'?`Пакет «${t.name}»`: `${sold} кадр. × ${price.toLocaleString('ru-RU')} ₽`}${discounted?' со скидкой 50% на фотографии':''} = ${photoAmount.toLocaleString('ru-RU')} ₽.${album?` Альбом «${album.name}»: ${albumCount} × ${Number(album.price).toLocaleString('ru-RU')} ₽ = ${albumAmount.toLocaleString('ru-RU')} ₽.`:''} Итого продажа: ${amount.toLocaleString('ru-RU')} ₽. Подтверждённые поступления по съёмке: ${paid.toLocaleString('ru-RU')} ₽. Предоплата по записи: ${Number(b.declaredDeposit||0).toLocaleString('ru-RU')} ₽; подтверждённая часть уже входит в поступления и повторно не вычитается. Остаток с учётом прежних покупок: ${due.toLocaleString('ru-RU')} ₽.${creditAmount>0?` Переплата / к возврату после сверки: ${creditAmount.toLocaleString('ru-RU')} ₽. Автоматического возврата нет; решение фиксирует руководитель.`:''}`;
 return amount===null?null:{amount,due,creditAmount,tariffId:fixed?.tariffId||t?.tariffId||t?.id||null,
  discountId,albumId:savedAlbum?.albumId||album?.id||null,albumQuantity:album?albumCount:null};
}
function refreshSalePricing(){
 const form=document.querySelector('#workflowSale');
 const booking=state?.bookings?.find(row=>row.id===Number(form?.dataset.draftScope));
 if(!form||!booking||form.dataset.pendingKeys||booking.draft?.pricing)return;
 const template=document.createElement('div');template.innerHTML=tariffOptions(booking,state.tariffs);
 let changed=false;
 for(const name of ['tariffId','albumId']){
  const select=form.elements[name],replacement=template.querySelector(`[name="${name}"]`);
  if(!select||!replacement)continue;
  const value=select.value;select.innerHTML=replacement.innerHTML;
  if([...select.options].some(option=>option.value===value))select.value=value;
  else{select.value='';changed=changed||Boolean(value);}
 }
 let notice=form.querySelector('[data-pricing-selection-notice]');
 if(changed){if(!notice){notice=document.createElement('p');notice.dataset.pricingSelectionNotice='';notice.setAttribute('role','status');form.append(notice);}notice.textContent='Выбранный тариф или альбом изменён. Выберите актуальный вариант и проверьте сумму перед сохранением продажи.';}
 updatePricePreview(form,booking,state.tariffs);
}
document.addEventListener('click',event=>{
 const button=event.target.closest('[data-tariff-edit],[data-album-edit]');if(!button)return;
 const album=button.dataset.albumEdit!==undefined;
 const item=(album?state?.albums:state?.tariffs)?.find(row=>row.id===Number(album?button.dataset.albumEdit:button.dataset.tariffEdit));
 const form=document.querySelector('#hotelPricingForm');if(!item||!form)return;
 const tariff=album?{...item,kind:'ALBUM',photoCount:null}:item;
 for(const key of ['hotelId','name','kind','price','photoCount'])form.elements[key].value=tariff[key]??'';
 form.elements.originalKind.value=tariff.kind;form.elements.replaces.value=tariff.id;form.scrollIntoView({block:'center'});
});
document.addEventListener('change',event=>{const form=event.target.form;if(form?.id!=='hotelPricingForm'||event.target.name!=='kind')return;if(form.elements.originalKind.value&&form.elements.originalKind.value!==event.target.value)form.elements.replaces.value='';if(event.target.value!=='PACKAGE')form.elements.photoCount.value='';});
document.addEventListener('submit',async event=>{
 const form=event.target;if(form.id!=='hotelPricingForm')return;event.preventDefault();
 const fields=new FormData(form),button=form.querySelector('button'),catalogue=state;button.disabled=true;
 try{
  const body={hotelId:Number(fields.get('hotelId')),name:fields.get('name'),kind:fields.get('kind'),price:fields.get('price'),photoCount:fields.get('kind')==='PACKAGE'&&fields.get('photoCount')?Number(fields.get('photoCount')):null,replaces:fields.get('replaces')?Number(fields.get('replaces')):null};
  const saved=await api('/hotel-tariffs',{method:'POST',body}),list=body.kind==='ALBUM'?'albums':'tariffs';
  catalogue[list]=(catalogue[list]||[]).filter(row=>row.id!==body.replaces&&!(body.kind==='UNIT'&&row.kind==='UNIT'&&row.hotelId===body.hotelId));
  catalogue[list].push({...body,id:saved.id});
  if(body.kind==='UNIT'){const hotel=catalogue.hotels.find(row=>row.id===body.hotelId);if(hotel){hotel.fixedUnitTariffId=saved.id;hotel.unitPrice=body.price;}}
  // A pending response belongs to the catalogue that initiated the save.
  if(state!==catalogue)return;
  const rows=document.querySelector('#hotelPricingRows');if(rows)rows.innerHTML=pricingRows(catalogue);
  refreshSalePricing();
  const note=document.querySelector('#bookingFixedPrice'),hotelId=document.querySelector('#workflowBooking')?.elements.hotel_id?.value;
  if(note)note.textContent=bookingPriceNote(catalogue,hotelId);
  if(form.isConnected){form.reset();form.querySelector('[role=status]').textContent=body.kind==='ALBUM'?'Цена альбома сохранена. Согласованные продажи сохраняют прежнюю цену.':'Цена сохранена для новых записей; прежние записи сохраняют свою цену.';}
  document.dispatchEvent(new CustomEvent('pb-pricing-updated'));
 }catch(error){if(form.isConnected)form.querySelector('[role=status]').textContent=error.message;}
 finally{button.disabled=false;}
});
