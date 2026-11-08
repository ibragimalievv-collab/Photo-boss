import {api} from './api.js';
import {esc} from './domain.js';

let actor=null,revision=0,snapshot=null,failure='',busy=false,loadedAt=0,serverAt=0;
const shiftNames={ON_SHIFT:'На смене',OFF_SHIFT:'Не на смене',REVIEW:'Отметка проверяется',FINISHED:'Смена завершена'};
export function widgetCountdown(value,now=Date.now()){
 const start=Date.parse(value?.nextBooking?.startsAt);if(!Number.isFinite(start))return '';
 const minutes=Math.ceil((start-now)/60000);
 if(value.nextBooking.status==='SHOOTING')return 'Съёмка идёт';
 if(minutes<=0)return 'Время съёмки подошло';
 if(minutes<60)return `через ${minutes} мин`;
 if(minutes<1440)return `через ${Math.floor(minutes/60)} ч${minutes%60?' '+minutes%60+' мин':''}`;
 return new Intl.DateTimeFormat('ru-RU',{timeZone:value.timezone||'Europe/Moscow',day:'numeric',month:'short'}).format(new Date(start));
}
const currentTime=()=>snapshot?serverAt+(performance.now()-loadedAt):Date.now();
export function resetStaffWidget(uid=null){actor=uid;revision++;snapshot=null;failure='';busy=false;paint();}
export function validWidgetSnapshot(value,uid){
 const generated=Date.parse(value?.generatedAt),until=Date.parse(value?.validUntil),next=value?.nextBooking;
 if(value?.version!==1||value.employee?.id!==uid||typeof value.employee.name!=='string'||typeof value.employee.role!=='string'||
  !Number.isFinite(generated)||!Number.isFinite(until)||until<=generated||until-generated>900000||
  !Number.isSafeInteger(value.today?.total)||!Number.isSafeInteger(value.today?.completed)||value.today.completed<0||value.today.total<value.today.completed)return false;
 try{new Intl.DateTimeFormat('ru-RU',{timeZone:value.timezone}).format();}catch{return false;}
 return next===null||(Number.isSafeInteger(next?.id)&&next.id>0&&Number.isSafeInteger(next.guestCount)&&next.guestCount>0&&
  Number.isFinite(Date.parse(next.startsAt))&&/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(next.time)&&
  typeof next.hotel==='string'&&(next.meetingPoint===null||typeof next.meetingPoint==='string')&&
  !['CANCELLED','REJECTED'].includes(next.status)&&next.url===`/app/#shootings?booking=${next.id}`);
}
export function staffWidgetView(value=snapshot,error=failure,now=currentTime()){
 const next=value?.nextBooking,expired=value&&Date.parse(value.validUntil)<=now;
 const name=value?.employee?.name||'',initial=name.trim().slice(0,1)||'•';
 const status=expired?'Нужно обновление':shiftNames[value?.shift]||'Обновляем данные';
 const href=next&&/^\/app\/#shootings\?booking=[1-9][0-9]*$/.test(next.url)?next.url:'/app/#shootings';
 return `<section class="staff-widget" aria-label="Ближайшая съёмка" data-staff-widget>
 <header class="staff-widget-brand"><img class="staff-widget-mark" src="/app/assets/widget-aperture.svg" alt=""><span>PHOTO BOSS</span><span class="staff-widget-avatar" aria-hidden="true">${esc(initial)}</span></header>
 <div class="staff-widget-role"><span>График${value?' · '+esc(value.employee.role):''}</span><span class="staff-widget-shift ${!expired&&value?.shift==='ON_SHIFT'?'is-working':''}">${esc(status)}</span></div>
 <div class="staff-widget-divider"></div>
 <div class="staff-widget-eyebrow">${next?'Ближайшая съёмка':value?'Ваш рабочий день':'Ближайшая съёмка'}</div>
 ${next?`<div class="staff-widget-clock"><strong>${esc(next.time)}</strong><span data-widget-countdown>${esc(widgetCountdown(value,now))}</span></div><h2>${esc(next.hotel)}</h2><p class="staff-widget-guests">Гостей: ${esc(next.guestCount)}</p><p class="staff-widget-place">⌖ ${esc(next.meetingPoint||'Место встречи уточните у менеджера')}</p>`:`<h2>${value?'Новых съёмок пока нет':error?'Не удалось обновить':'Загружаем вашу съёмку…'}</h2><p class="staff-widget-guests">${value?'Назначенная съёмка появится здесь.':'Записи и рабочие файлы сохраняются в приложении.'}</p>`}
 ${error||expired?`<p class="staff-widget-warning" role="status">${esc(error||'Данные могли измениться. Откройте запись или обновите карточку.')}</p>`:''}
 <div class="staff-widget-actions"><a class="staff-widget-open" href="${href}">${next?'Открыть запись':'Открыть съёмки'} <span aria-hidden="true">→</span></a><button type="button" data-widget-chat aria-label="Открыть рабочий чат">◌</button><button type="button" data-widget-refresh aria-label="Обновить ближайшую съёмку" ${busy?'disabled':''}>↻</button></div>
 <footer>${value?`✓ Сегодня: ${Number(value.today.completed)} из ${Number(value.today.total)} съёмок завершено`:'Данные обновляются с сервера'}</footer></section>`;
}
function paint(){const box=document.querySelector('#staffWidgetMount');if(box)box.innerHTML=staffWidgetView();}
export async function refreshStaffWidget(){
 if(actor===null||busy||document.hidden||!document.querySelector('#staffWidgetMount'))return;
 const ticket=revision,uid=actor;busy=true;
 try{const value=await api('/widget',{allowSnapshot:false,timeoutMs:12000});
  if(ticket!==revision||uid!==actor)return;
  if(!validWidgetSnapshot(value,uid))throw Error('Не удалось проверить данные вашей съёмки.');
  snapshot=value;serverAt=Date.parse(value.generatedAt);loadedAt=performance.now();failure='';
 }catch(error){if(ticket===revision){if(error.status===401||error.status===403)snapshot=null;failure=error.message||'Нет связи. Обновите карточку после подключения.';}}
 finally{if(ticket===revision){busy=false;paint();}}
}
export function mountStaffWidget(uid){
 if(window.PHOTO_BOSS_CONFIG?.STANDALONE!==true)return;
 if(actor!==uid)resetStaffWidget(uid);
 paint();void refreshStaffWidget();
}
document.addEventListener('click',event=>{
 if(event.target.closest?.('[data-widget-refresh]'))void refreshStaffWidget();
 if(event.target.closest?.('[data-widget-chat]'))document.dispatchEvent(new CustomEvent('pb-work-chat-open'));
});
document.addEventListener('pb-chat-account-reset',()=>resetStaffWidget());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshStaffWidget();});
window.addEventListener('online',()=>void refreshStaffWidget());
document.addEventListener('pb-assignment-action-completed',()=>void refreshStaffWidget());
setInterval(()=>void refreshStaffWidget(),30000);
setInterval(()=>{const node=document.querySelector('[data-widget-countdown]');if(node&&snapshot)node.textContent=widgetCountdown(snapshot,currentTime());},1000);
