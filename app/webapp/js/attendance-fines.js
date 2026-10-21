import {api} from './api.js';
import {esc} from './domain.js';
let data=null;
const clock=value=>value?new Date(value).toLocaleString('ru-RU'):'Нет отметки';
export async function attendanceFinesView(focusKey=null){
 data=await api('/attendance/fines',{allowSnapshot:false});
 const field=(name,label,value,optional=false)=>`<label class="field">${label}<input class="input" type="number" name="${name}" min="0" max="1000000" step=".01" value="${value??''}" ${optional?'placeholder="Сумма ещё не задана"':'required'}></label>`;
 return `<p>Приход и уход сравниваются с графиком. Без графика при отправке прихода действует начало в 09:00. Расчёт работает автоматически, независимо от проверки фото. Уважительную причину можно учесть ниже; каждое изменение сохраняется в истории.</p>${data.canEditRules?`<details class="panel"><summary>Правила автоматического расчёта</summary><form class="attendance-fine-rules form-grid">${field('lateAmount','Опоздание / нет прихода вовремя, ₽',data.rules.lateAmount)}${field('earlyAmount','Ранний уход, ₽',data.rules.earlyAmount,true)}${field('missingEndAmount','Нет отметки ухода, ₽',data.rules.missingEndAmount,true)}<p>Пустая сумма: событие фиксируется, денежный штраф не назначен. Новые суммы применяются к новым событиям; старые начисления меняйте отдельно.</p><button class="btn primary" type="submit">Сохранить правила</button><p role="status"></p></form></details>`:''}${data.items.map((r,index)=>`<details class="panel section" ${focusKey===`attendance:fine:${r.date}:${r.userId}:${r.kind}`?'open':''}><summary>${esc(r.name)} · ${esc(r.date)} · ${esc(r.label)} · ${r.amount} ₽</summary><p>По графику: ${esc(clock(r.expectedAt))}<br>Отметка: ${esc(clock(r.observedAt))}<br>Расчёт: ${r.calculatedAmount} ₽${!r.ruleConfigured?' · сумма правила не задана':''}</p>${r.override?`<p>Исправлено: ${esc(r.override.reason)}</p>`:''}<form class="attendance-fine-edit form-grid" data-fine="${index}">${field('amount','Итоговый штраф, ₽',r.amount)}<label class="field">Причина исправления<textarea class="input" name="reason" minlength="3" maxlength="1000" required></textarea></label><button class="btn primary" type="submit">Сохранить исключение</button><p role="status"></p></form></details>`).join('')||'<p>Начислений пока нет.</p>'}`;
}
document.addEventListener('submit',async event=>{
 const form=event.target;if(!form.matches('.attendance-fine-edit,.attendance-fine-rules'))return;
 event.preventDefault();const fields=Object.fromEntries(new FormData(form)),button=form.querySelector('button'),note=form.querySelector('[role=status]');button.disabled=true;
 try{
  if(form.classList.contains('attendance-fine-rules')){
   await api('/attendance/fine-rules',{method:'PUT',body:{revision:data.rulesRevision,rules:{lateAmount:Number(fields.lateAmount),earlyAmount:fields.earlyAmount===''?null:Number(fields.earlyAmount),missingEndAmount:fields.missingEndAmount===''?null:Number(fields.missingEndAmount)}}});
  }else{
   const r=data.items[Number(form.dataset.fine)];
   await api('/attendance/fines',{method:'PUT',body:{userId:r.userId,date:r.date,kind:r.kind,amount:Number(fields.amount),reason:fields.reason,revision:r.revision}});
  }
  const panel=document.querySelector('#sheetContent');panel.innerHTML='<h2>Штрафы и исключения</h2>'+await attendanceFinesView();
 }catch(error){note.textContent=error.message;button.disabled=false;}
});
