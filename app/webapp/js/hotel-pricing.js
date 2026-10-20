import {api} from './api.js';
import {esc} from './domain.js';
let state;

export function bookingPriceNote(data,hotelId){
 const hotel=(data.hotels||[]).find(h=>h.id===Number(hotelId));
 const rates=(data.tariffs||[]).filter(t=>t.hotelId===Number(hotelId)&&t.kind==='UNIT');
 const selected=rates.find(t=>t.id===hotel?.fixedUnitTariffId)||(rates.length===1?rates[0]:null);
 const price=hotel?.unitPrice??selected?.price;
 if(price!=null)return `Цена отеля: ${Number(price).toLocaleString('ru-RU')} ₽ за кадр. Сохраняется в записи и не меняется при продаже.`;
 if(rates.length>1)return 'Владелец или администратор должен закрепить одну цену кадра для этого отеля. Случайный тариф не применяется.';
 return 'Цена выбранного вида съёмки сохранится при создании записи. Владелец или администратор может закрепить цену за отелем.';
}
export function pricingView(data){
 state=data;if(!data.canConfigurePricing)return '';
 return `<details class="panel section"><summary>Фиксированные цены отелей и пакеты</summary><p>У каждого отеля одна цена кадра. Задавать её может владелец или администратор своего отеля. Изменение применяется только к новым записям; согласованные цены прежних записей и продаж сохраняются. Пустое число кадров у пакета означает всю съёмку.</p><form id="hotelPricingForm" class="form-grid"><input type="hidden" name="replaces"><label class="field">Отель<select class="input" name="hotelId">${data.hotels.map(h=>`<option value="${h.id}">${esc(h.name)}</option>`).join('')}</select></label><label class="field">Название<input class="input" name="name" minlength="2" maxlength="150" required></label><label class="field">Тип<select class="input" name="kind"><option value="UNIT">Фиксированная цена одного кадра в отеле</option><option value="PACKAGE">Пакет за фиксированную сумму</option></select></label><label class="field">Цена, ₽<input class="input" name="price" type="number" min="0.01" step="0.01" required></label><label class="field">Кадров в пакете (пусто — вся съёмка)<input class="input" name="photoCount" type="number" min="1" max="10000"></label><button class="btn primary">Сохранить тариф</button><p role="status"></p></form>${data.hotels.map(h=>`<p>${esc(h.name)} · ${esc(bookingPriceNote(data,h.id))}</p>`).join('')}${(data.tariffs||[]).map(t=>`<p>${esc(data.hotels.find(h=>h.id===t.hotelId)?.name)} · ${esc(t.name)} · ${esc(t.price)} ₽ ${t.kind==='UNIT'?'за кадр':`за ${t.photoCount||'все'} фото`} <button type="button" class="btn ghost" data-tariff-edit="${t.id}">Изменить</button></p>`).join('')}</details>`;
}
function bookingUnit(b){
 return b.pricing||(Number(b.price)>0?{tariffId:null,kind:'UNIT',price:b.price,name:'Цена записи',photoCount:null}:null);
}
export function tariffOptions(b,tariffs){
 const fixed=b.draft?.pricing,unit=bookingUnit(b),packages=(tariffs||[]).filter(t=>t.hotelId===b.hotelId&&t.kind==='PACKAGE');
 const options=fixed?`<option value="${fixed.tariffId||0}" selected>Тариф сохранён · ${esc(fixed.kind==='PACKAGE'?fixed.name:`${fixed.price} ₽/кадр`)}</option>`:
 `${unit?`<option value="${unit.tariffId||0}" selected>Поштучно · ${esc(unit.price)} ₽ за кадр · цена записи</option>`:'<option value="" selected disabled>Цена записи требует проверки владельцем</option>'}${packages.map(t=>`<option value="${t.id}">Пакет «${esc(t.name)}» · ${esc(t.price)} ₽ за ${t.photoCount||'все'} фото</option>`).join('')}`;
 return `<label class="field">Способ расчёта<select class="input" name="tariffId" ${fixed?'disabled':'required'}>${options}</select></label><div class="notice" id="salePricePreview" role="status">Укажите количество: приложение покажет сумму продажи, поступления и доплату.</div>`;
}
export function updatePricePreview(form,b,tariffs){
 if(!form||!b)return;
 const fixed=b.draft?.pricing,unit=bookingUnit(b),key=Number(form.elements.tariffId?.value);
 const t=fixed||(unit&&key===(unit.tariffId||0)?unit:(tariffs||[]).find(t=>t.id===key&&t.hotelId===b.hotelId&&t.kind==='PACKAGE'));
 const sold=Number(form.elements.sold?.value||0),price=Number(t?.price||0);
 const amount=t?.kind==='PACKAGE'?price:t?.kind==='UNIT'&&Number.isInteger(sold)&&sold>0?Math.round(sold*price*100)/100:null;
 const paid=Number(b.paidAmount||0),due=amount===null?null:Math.max(0,Math.round((Number(b.salesAmount||0)+amount-paid)*100)/100);
 const box=form.querySelector('#salePricePreview');
 if(box)box.textContent=amount===null?'Проверьте цену записи, способ расчёта и количество кадров.':`${t.kind==='PACKAGE'?`Пакет «${t.name}»`: `${sold} кадр. × ${price.toLocaleString('ru-RU')} ₽`} = ${amount.toLocaleString('ru-RU')} ₽. Подтверждённые поступления по съёмке: ${paid.toLocaleString('ru-RU')} ₽. Предоплата по записи: ${Number(b.declaredDeposit||0).toLocaleString('ru-RU')} ₽; подтверждённая часть уже входит в поступления и повторно не вычитается. Остаток с учётом прежних покупок: ${due.toLocaleString('ru-RU')} ₽.`;
 return amount===null?null:{amount,due,tariffId:fixed?.tariffId||t?.tariffId||t?.id||null};
}
document.addEventListener('click',event=>{const b=event.target.closest('[data-tariff-edit]');if(!b)return;const t=state.tariffs.find(t=>t.id===Number(b.dataset.tariffEdit)),f=document.querySelector('#hotelPricingForm');for(const k of ['hotelId','name','kind','price','photoCount'])f.elements[k].value=t[k]??'';f.elements.replaces.value=t.id;f.scrollIntoView({block:'center'});});
document.addEventListener('submit',async event=>{const form=event.target;if(form.id!=='hotelPricingForm')return;event.preventDefault();const f=new FormData(form),button=form.querySelector('button');button.disabled=true;try{await api('/hotel-tariffs',{method:'POST',body:{hotelId:Number(f.get('hotelId')),name:f.get('name'),kind:f.get('kind'),price:f.get('price'),photoCount:f.get('photoCount')?Number(f.get('photoCount')):null,replaces:f.get('replaces')?Number(f.get('replaces')):null}});form.reset();form.querySelector('[role=status]').textContent='Цена сохранена для новых записей. Обновите рабочие операции; прежние записи сохраняют свою цену.';}catch(error){form.querySelector('[role=status]').textContent=error.message;}finally{button.disabled=false;}});
