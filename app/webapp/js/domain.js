export const THEMES = ['premium','light','photo'];
export const ROLE_NAMES = { OWNER:'Владелец', ADMIN:'Администратор', PHOTOGRAPHER:'Фотограф', MANAGER:'Менеджер записи' };
export const STATUS_NAMES = {NEW:'Новая запись',ASSIGNED:'Назначена',ACCEPTED:'Принята',ARRIVED:'На месте',IN_PROGRESS:'Идёт съёмка',COMPLETED:'Завершена',PICKED_UP:'Принята',SHOOTING:'Идёт съёмка',SHOT:'Съёмка завершена',PROCESSING:'На обработке',READY_FOR_SALE:'К продаже',SOLD:'Продана',REJECTED:'Отклонена',CANCELLED:'Отменена'};
export function permissions(roles){
 const owner=roles.includes('OWNER'), admin=roles.includes('ADMIN');
 return {financeScope:owner?'all':admin?'today':'self',audit:owner,manageSchedule:owner||admin,manageBookings:owner||admin||roles.includes('MANAGER')};
}
export function defaultTheme(roles){return roles.includes('OWNER')?'premium':'light';}
export function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
export function money(kopeks){if(!Number.isSafeInteger(kopeks))return '—';return new Intl.NumberFormat('ru-RU',{maximumFractionDigits:kopeks%100?2:0,minimumFractionDigits:kopeks%100?2:0}).format(kopeks/100)+' ₽';}
export function todayKey(){return new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
export function addDays(day,n){const date=new Date(`${day}T12:00:00Z`);date.setUTCDate(date.getUTCDate()+n);return date.toISOString().slice(0,10);}
export function periodRange(period,today=todayKey()){
 const d=new Date(today+'T12:00:00Z');
 if(period==='week')return [addDays(today,-((d.getUTCDay()+6)%7)),today];
 if(period==='month')return [today.slice(0,8)+'01',today];
 return [today,today];
}
export function dateLabel(day,options={day:'numeric',month:'long'}){return new Intl.DateTimeFormat('ru-RU',{...options,timeZone:'Europe/Moscow'}).format(new Date(day+'T12:00:00Z'));}
export function initials(name){return name.split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('');}
export function computeCommission(amountKopeks,frames){
 if(!Number.isSafeInteger(amountKopeks)||amountKopeks<0||!Number.isInteger(frames)||frames<0)throw new Error('Некорректные данные');
 const rate=frames>=150?15:10;
 const bonus=amountKopeks>=2100000?100000+Math.floor((amountKopeks-2100000)/500000)*50000:0;
 return {rate,commission:Math.round(amountKopeks*rate/100),bonus};
}
export function maySeeFinancialPeriod(roles,period){return !(roles.includes('ADMIN')&&!roles.includes('OWNER')&&period!=='today');}
export function overlap(a,b){return a.start<b.end&&b.start<a.end;}
export function validAmount(value){const s=String(value).trim().replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(s))throw new Error('Введите сумму с точностью до копейки');const n=Math.round(Number(s)*100);if(!Number.isSafeInteger(n)||n<=0||n>1000000000)throw new Error('Сумма должна быть от 0,01 до 10 000 000 ₽');return n;}
