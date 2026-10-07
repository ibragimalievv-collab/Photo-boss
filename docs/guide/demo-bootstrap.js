window.PB_DEMO_ROLE="OWNER";(()=>{const THEMES = ['premium','light','photo'];
const ROLE_NAMES = { OWNER:'Владелец', ADMIN:'Администратор', PHOTOGRAPHER:'Фотограф', MANAGER:'Менеджер записи' };
const STATUS_NAMES = {NEW:'Новая запись',ASSIGNED:'Назначена',ACCEPTED:'Принята',ARRIVED:'На месте',IN_PROGRESS:'Идёт съёмка',COMPLETED:'Завершена',READY_FOR_SALE:'К продаже',SOLD:'Продана',REJECTED:'Отклонена',CANCELLED:'Отменена'};
function permissions(roles){
 const owner=roles.includes('OWNER'), admin=roles.includes('ADMIN');
 return {financeScope:owner?'all':admin?'today':'self',audit:owner,manageSchedule:owner||admin,manageBookings:owner||admin||roles.includes('MANAGER')};
}
function defaultTheme(roles){return roles.includes('OWNER')?'premium':'light';}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function money(kopeks){if(!Number.isSafeInteger(kopeks))return '—';return new Intl.NumberFormat('ru-RU',{maximumFractionDigits:kopeks%100?2:0,minimumFractionDigits:kopeks%100?2:0}).format(kopeks/100)+' ₽';}
function todayKey(){return new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
function addDays(day,n){const date=new Date(`${day}T12:00:00Z`);date.setUTCDate(date.getUTCDate()+n);return date.toISOString().slice(0,10);}
function periodRange(period,today=todayKey()){
 const d=new Date(today+'T12:00:00Z');
 if(period==='week')return [addDays(today,-((d.getUTCDay()+6)%7)),today];
 if(period==='month')return [today.slice(0,8)+'01',today];
 return [today,today];
}
function dateLabel(day,options={day:'numeric',month:'long'}){return new Intl.DateTimeFormat('ru-RU',{...options,timeZone:'Europe/Moscow'}).format(new Date(day+'T12:00:00Z'));}
function initials(name){return name.split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('');}
function computeCommission(amountKopeks,frames){
 if(!Number.isSafeInteger(amountKopeks)||amountKopeks<0||!Number.isInteger(frames)||frames<0)throw new Error('Некорректные данные');
 const rate=frames>=150?15:10;
 const bonus=amountKopeks>=2100000?100000+Math.floor((amountKopeks-2100000)/500000)*50000:0;
 return {rate,commission:Math.round(amountKopeks*rate/100),bonus};
}
function maySeeFinancialPeriod(roles,period){return !(roles.includes('ADMIN')&&!roles.includes('OWNER')&&period!=='today');}
function overlap(a,b){return a.start<b.end&&b.start<a.end;}
function validAmount(value){const s=String(value).trim().replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(s))throw new Error('Введите сумму с точностью до копейки');const n=Math.round(Number(s)*100);if(!Number.isSafeInteger(n)||n<=0||n>1000000000)throw new Error('Сумма должна быть от 0,01 до 10 000 000 ₽');return n;}




class ApiError extends Error {constructor(message,status=0){super(message);this.status=status;}}
function preferBrowserSession(){}
const role=window.PB_DEMO_ROLE||'OWNER', today=todayKey();
const id={OWNER:1,ADMIN:2,PHOTOGRAPHER:3,MANAGER:5}[role];
const names={OWNER:'Демо Владелец',ADMIN:'Демо Администратор',PHOTOGRAPHER:'Демо Фотограф',MANAGER:'Демо Менеджер'};
const people=[{id:3,name:'Демо Фотограф',role:'PHOTOGRAPHER'},{id:4,name:'Тестовый Фотограф',role:'PHOTOGRAPHER'},{id:5,name:'Демо Менеджер',role:'MANAGER'}];
const hotels=[{id:1,name:'Демо Отель у моря'},{id:2,name:'Демо Парк-отель'}];
const shifts=Array.from({length:7},(_,d)=>people.flatMap((p,i)=>[{id:100+d*10+i,userId:p.id,name:p.name,role:p.role,hotelId:1,hotel:hotels[0].name,date:addDays(today,d),start:'09:00',end:p.id===3?'12:00':'18:00',status:'PLANNED',attendance:d===0?'STARTED':'PLANNED'},...(p.id===3?[{id:200+d,userId:3,name:p.name,role:p.role,hotelId:2,hotel:hotels[1].name,date:addDays(today,d),start:'13:00',end:'18:00',status:'PLANNED',attendance:'PLANNED'}]:[])]));
const bookings=Array.from({length:6},(_,i)=>({id:501+i,client:['Демо Семья 1','Демо Пара 2','Демо Гость 3','Демо Семья 4','Демо Пара 5','Демо Гость 6'][i],date:today,time:['09:30','10:30','11:30','13:30','15:00','16:00'][i],type:'Фотосессия',room:String(201+i),hotel:hotels[i%2].name,hotelId:i%2+1,photographer:i%2?people[1].name:people[0].name,photographerId:i%2?4:3,managerId:5,status:['ASSIGNED','IN_PROGRESS','COMPLETED','READY_FOR_SALE','SOLD','NEW'][i],frames:i>1?150:0}));
const entries=people.map((p,i)=>({...p,sales:[2600000,1200000,2600000][i],commission:[390000,120000,390000][i],adjustments:i===0?150000:0,earned:[540000,120000,390000][i]}));
function finance(q){const own=!['OWNER','ADMIN'].includes(role),employees=own?entries.filter(e=>e.id===id):entries;return {period:q.get('period')||'today',from:today,to:today,scope:own?'self':'company',sales:own?employees[0].sales:3800000,cashReceived:own?null:3800000,payroll:employees.reduce((s,e)=>s+e.earned,0),employees,salesRows:[{id:601,bookingId:505,client:'Демо Гость 6',employee:own?names[role]:'Демо Фотограф',date:today,amount:own?employees[0].sales:2600000,status:'PAID'}],salesCount:1,salesListLimited:false,note:'Вымышленные данные для просмотра. Все периоды показывают один тестовый набор. Это не рабочая касса.'};}
const ownBookings=()=>bookings.filter(b=>role!=='PHOTOGRAPHER'||b.photographerId===id);
const lessons=[{slug:'demo-light',title:'Демо: мягкий свет',body:'Учебный пример: найдите мягкий боковой свет, проверьте фокус на глазах и фон. Это демонстрационный урок, не полный курс.',day:1,block:1,locked:false,duration:'Демо-урок'},{slug:'demo-series',title:'Демо: разные планы',body:'Учебный пример: общий план, средний план и детали. Не повторяйте один ракурс.',day:2,block:1,locked:false,duration:'Демо-урок'}];
async function api(path,{method='GET',body}={}){
 const u=new URL(path,'https://demo.invalid'),p=u.pathname,q=u.searchParams;
 if(method!=='GET'){
  if(p==='/session')return {ok:true};
  if(p==='/preferences')return {theme:body.theme};
  throw new ApiError('Демо доступно только для просмотра. Рабочие данные не изменяются.',409);
 }
 if(p==='/me')return {user:{id,telegramId:900000000+id,name:names[role],roles:[role],theme:role==='OWNER'?'premium':'light'},permissions:permissions([role]),screenCaptureAllowed:true,passwordConfigured:false,passwordLogin:'',today,timezone:'Europe/Moscow',mode:'demo'};
 if(p==='/dashboard')return {today,team:shifts.filter(s=>s.date===today),bookings:ownBookings(),finance:finance(q),updatedAt:new Date().toISOString()};
 if(p==='/finance')return finance(q);
 if(p==='/bookings')return {items:q.get('day')&&q.get('day')!==today?[]:ownBookings()};
 if(p==='/schedule')return {items:shifts.filter(s=>(!q.get('from')||s.date>=q.get('from'))&&(!q.get('to')||s.date<=q.get('to'))&&(['OWNER','ADMIN'].includes(role)||s.userId===id)),employees:people.map(({id,name})=>({id,name})),hotels};
 if(p==='/audit')return {items:[{id:1,at:new Date().toISOString(),actor:'Демо Владелец',title:'Пример: назначена смена',details:JSON.stringify({before:null,after:{hotel:'Демо Отель у моря',time:'09:00–12:00'}}),entity:'Смена',entityId:100,action:'miniapp_shift_created'}],next:null};
 if(p==='/academy')return {lessons,blocks:[{number:1,title:'Демо: начало обучения',locked:false,lessonsDone:1,lessonsTotal:2,practiceDone:false,practiceTitle:'Пять разных кадров'}],currentBlock:1,completed:['demo-light'],points:10,practices:[],reviews:[],personalTip:null,certificate:null,locations:[],team:[]};
 throw new ApiError('Этот раздел не подключён в демо. Доступны Сегодня, Съёмки, График, Академия, Касса, Аудит и Профиль.',409);
}

window.pbDemoApi=api;})();