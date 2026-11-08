import {esc,money,dateLabel,plural} from './domain.js';
import {icon} from './icons.js';

const cancelled=new Set(['CANCELLED','REJECTED']);
const finished=new Set(['SHOT','READY_FOR_SALE','COMPLETED','SOLD']);
const startsAt=booking=>Date.parse(`${booking.date}T${booking.time}:00+03:00`);
export function dayOverview(data,now=Date.parse(data.updatedAt)||Date.now()){
 const bookings=data.bookings||[],active=bookings.filter(row=>!cancelled.has(row.status));
 const waiting=active.filter(row=>!finished.has(row.status));
 const overdue=waiting.filter(row=>startsAt(row)<now).sort((a,b)=>startsAt(a)-startsAt(b));
 const future=waiting.filter(row=>startsAt(row)>=now).sort((a,b)=>startsAt(a)-startsAt(b));
 const first=future[0],next=first?future.filter(row=>startsAt(row)===startsAt(first)):[];
 const people=[...new Map((data.team||[]).map(row=>[row.userId??row.id,row])).values()];
 return {active,overdue,next,completed:active.filter(row=>finished.has(row.status)).length,
  cancelled:bookings.length-active.length,people,onShift:people.filter(row=>row.attendance==='STARTED').length,
  minutesLate:row=>Math.max(0,Math.floor((now-startsAt(row))/60000)),
  untilNext:first?Math.max(0,Math.ceil((startsAt(first)-now)/60000)):null};
}

export function homeSummaryView(data,permissions){
 const day=dayOverview(data),attention=data.attention||{},alerts=[];
 for(const row of day.overdue.slice(0,3))alerts.push({tone:'danger',icon:'clock',route:`shootings?booking=${row.id}`,
  title:`Съёмка ${row.time}: ${['SHOOTING','IN_PROGRESS'].includes(row.status)?'идёт съёмка':'нужен статус'}`,
  description:`${day.minutesLate(row)} мин после назначенного времени · ${row.hotel} · ${row.client}. Проверьте статус.`});
 for(const row of attention.unfinishedSales||[])alerts.push({tone:'warning',icon:'wallet',route:`workflow?booking=${row.bookingId}`,
  title:`Продажа №${row.id} не завершена`,description:`${row.hotel} · ${row.client} · оформляет ${row.operator}.`});
 if(permissions.manageSchedule&&attention.pendingAttendance>0)alerts.push({tone:'warning',icon:'pin',route:'attendance',
  title:`${attention.pendingAttendance} ${plural(attention.pendingAttendance,['отметка','отметки','отметок'])} на проверке`,description:'Проверьте фотографии и время смены.'});
 for(const row of attention.awaitingSale||[])alerts.push({tone:'warning',icon:'wallet',route:`shootings?booking=${row.id}`,
  title:`Съёмка ${row.time} без продажи`,description:`${row.hotel} · ${row.client}. Оформите продажу или отметьте «Гость не купил».`});
 for(const row of attention.overdueBookings||[])alerts.push({tone:'warning',icon:'calendar',route:`shootings?booking=${row.id}`,
  title:`Не отмечен результат съёмки ${row.time}`,description:`${dateLabel(row.date,{day:'numeric',month:'short'})} · ${row.hotel} · ${row.client}.`});
 const f=data.finance||{},self=f.scope==='self',withholdings=f.deductions;
 const next=day.next[0],hotels=[...new Set(day.next.map(row=>row.hotel))];
 return `<div class="page-head home-review-heading"><div><div class="eyebrow">${esc(dateLabel(data.today,{weekday:'short',day:'numeric',month:'long'}))} · Москва</div><h1>${day.active.length} ${plural(day.active.length,['съёмка','съёмки','съёмок'])} сегодня</h1><p>${day.completed} завершено${day.cancelled?` · ${day.cancelled} ${plural(day.cancelled,['отмена','отмены','отмен'])}`:''}</p>${alerts.length?'<p class="home-attention-label">Требует внимания</p>':''}</div><button class="icon-button" data-action="refresh" aria-label="Обновить данные">${icon('refresh')}</button></div>
 <div class="home-attention-list">${alerts.slice(0,3).map(row=>`<button class="home-attention ${row.tone}" data-go="${esc(row.route)}">${icon(row.icon)}<span class="grow"><strong>${esc(row.title)}</strong><small>${esc(row.description)}</small></span>${icon('chevron')}</button>`).join('')}</div>
 ${alerts.length>3?'<button class="text-button section" data-go="control">Все незавершённые заказы</button>':''}
 <button class="home-shift-summary" data-go="${permissions.manageSchedule?'attendance':'schedule'}"><span class="dot positive"></span><span>На смене <strong>${day.onShift} из ${day.people.length}</strong>${permissions.manageSchedule&&attention.pendingAttendance>0?` · проверок: ${attention.pendingAttendance}`:''}</span><strong>Смены ${icon('arrow')}</strong></button>
 <div class="home-money-summary" data-sensitive="true"><button data-go="finance"><small>${self?'Начислено':'Поступило'}</small><strong>${money(self?f.grossPayroll:f.cashReceived)}</strong></button><button data-go="finance"><small>Продано</small><strong>${money(f.sales)}</strong></button><button data-${permissions.manageSchedule?'action="attendance-fines"':'go="finance"'}><small>Удержания</small><strong class="${withholdings?'negative':''}">${money(withholdings)}</strong></button></div>
 ${next?`<button class="home-next-shoot" data-go="shootings?booking=${next.id}"><span class="eyebrow">Следующая · ${day.untilNext===0?'сейчас':`через ${day.untilNext} мин`}</span><strong>${esc(next.time)} <span>${esc(hotels.join(', '))}${day.next.length>1?` · ${day.next.length} ${plural(day.next.length,['съёмка','съёмки','съёмок'])}`:''}</span></strong><small>${esc(day.next.map(row=>`${row.client}${row.photographer?' ('+row.photographer+')':''}`).join(' · '))}</small></button>`:`<div class="panel home-next-empty"><strong>${day.overdue.length?'Предстоящих съёмок сегодня больше нет':'Новых съёмок сегодня пока нет'}</strong><p>${day.overdue.length?'Незавершённые записи показаны выше.':'Новая бронь появится здесь после сохранения.'}</p></div>`}`;
}

export function moreSectionsView(me){
 const p=me.permissions,roles=me.user.roles,owner=roles.includes('OWNER'),manager=roles.some(role=>['OWNER','ADMIN'].includes(role));
 const item=(route,ic,title,subtitle,action=false)=>`<button class="menu-item" data-${action?'action':'go'}="${route}">${icon(ic)}<div class="grow"><strong>${title}</strong><small>${subtitle}</small></div>${icon('chevron')}</button>`;
 const group=(title,items)=>`<section class="more-section"><h2 class="eyebrow">${title}</h2><div class="panel menu-list">${items.join('')}</div></section>`;
 return `<h1>Все разделы</h1><div class="more-sections">
 ${group('Деньги',[item('finance','wallet',p.financeScope==='self'?'Мои начисления':'Касса и начисления','Продажи, поступления и начисления'),...(manager?[item('attendance-fines','shield','Штрафы и удержания','Проверить расчёт и указать исключение',true)]:[])])}
 ${group('Команда',[...(p.manageSchedule?[item('attendance','clock','Смены','Кто на смене и какие отметки проверить'),item('team','users','Сотрудники и HR','Команда, найм и развитие')]:[]),item('schedule','calendar','График','План смен фотографов и менеджеров'),item('workday','list','Итоги смены и обед','Отметки времени, отчёт и замечания'),item('academy','cap','Академия','Уроки, практика и прогресс'),item('development','cap','AI-развитие и продажи','Разбор фотографий и голосовая практика'),item('onboarding','list','Начало работы','Инструкции и тест по вашей роли')])}
 ${group('Гости и съёмки',[item('control','camera','Незавершённые заказы','Снять, продать, выдать'),item('galleries','camera','Галереи','Фотографии и выдача гостям'),...(owner?[item('contacts','list','Контакты гостей','Контакты и история сообщений')]:[]),item('workflow','list','Рабочие операции','Брони, продажи, загрузка и синхронизация')])}
 ${group('Аналитика и настройки',[...(p.audit?[item('insights','wallet','Контроль бизнеса','Главные показатели и сравнение периодов'),item('audit','shield','Аудит действий','Кто, когда и что изменил')]:[]),...(manager?[item('hotel-points','pin','Точки и зоны отелей','Проверка местоположения сотрудников',true)]:[]),item('profile','palette','Профиль и оформление','Личные настройки и вход')])}
 ${owner?`<details class="panel more-section"><summary>Диагностика и поддержка</summary>${item('ai-diagnostics','cap','Диагностика ИИ','Состояние подключения и результаты проверок',true)}${item('disk-audit','camera','Сверка фотографий','Проверить сохранность на Яндекс.Диске',true)}</details>`:''}</div>`;
}
