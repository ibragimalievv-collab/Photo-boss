import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const original=fs.readFileSync(new URL('../app/attendance_ui/attendance.js',import.meta.url),'utf8');
const source=original.replace(/^import .*;\n/gm,'').replaceAll('export ','').replace('refresh();card();',`
 globalThis.attendanceTest={openCamera,draw,refresh,
  prime(){data={eligible:true,date:new Date().toISOString().slice(0,10),timezone:'Europe/Moscow',start:null,end:null,reviewRequired:true};dialog.open=true;},
  configure(patch){data={...data,...patch};draw();},
  takePhoto(when=new Date().toISOString()){photo='data:image/jpeg;base64,Y2FtZXJh';photoAt=when;},
  state(){return {data,purpose,retakePurpose,point,pointSaved,photo,busy,cameraActive,cameraStream,failure,open:dialog.open,html:dialog.innerHTML};}
 };
`);

function fixture(respond=async()=>{throw new Error('unexpected request');}){
 const events=new Map(),windowEvents=new Map(),cameras=[],calls=[],queued=[],rows=[],sent=[];
 const dialog={open:false,innerHTML:'',setAttribute(){},addEventListener(){},querySelector(){return null;},close(){this.open=false;},showModal(){this.open=true;}};
 const root={querySelector(){return null;},prepend(){}};
 const document={hidden:false,querySelector:s=>s==='#app'?root:null,createElement:tag=>tag==='dialog'?dialog:{},head:{append(){}},body:{append(){}},addEventListener:(name,handler)=>events.set(name,handler)};
 const c=vm.createContext({console,Date,Number,Intl,Promise,Error,setTimeout,clearTimeout,document,location:{hash:''},
  Uint8Array,Blob,atob,
  window:{addEventListener:(name,handler)=>windowEvents.set(name,handler)},
  navigator:{onLine:true,geolocation:{getCurrentPosition:accept=>accept({coords:{latitude:44.9,longitude:37.3,accuracy:10}})},mediaDevices:{getUserMedia:()=>new Promise(resolve=>cameras.push(resolve))}},
  MutationObserver:class{observe(){}},api:async(path,options)=>{calls.push(path);sent.push(options?.body);return respond(path,options);},
  enqueueOperation:async(...args)=>queued.push(args),outboxRows:async()=>rows,esc:x=>x??'',requestAnimationFrame(){}});
 vm.runInContext(source,c);c.attendanceTest.prime();
 const click=act=>events.get('click')({target:{closest:()=>({dataset:{att:act}})},preventDefault(){},stopImmediatePropagation(){}});
 return {c,events,cameras,calls,queued,rows,sent,click};
}
const base=()=>({eligible:true,date:new Date().toISOString().slice(0,10),timezone:'Europe/Moscow',start:null,end:null,reviewRequired:true});
const network=()=>Object.assign(new Error('Сеть недоступна'),{status:503});

{
 let attempts=0;
 const f=fixture(async path=>{
  if(path==='/attendance/location'){
   if(++attempts===1)throw network();
   return {...base(),start:{status:'AWAITING_PHOTO',locationFresh:true,locationReceivedAt:new Date().toISOString()}};
  }
  assert.equal(path,'/attendance/photo');
  return {...base(),start:{status:'STARTED',at:new Date().toISOString()}};
 });
 await f.click('location');
 assert.equal(f.c.attendanceTest.state().pointSaved,false);
 assert.match(f.c.attendanceTest.state().html,/Сервер ещё не подтвердил сохранение/);
 f.c.attendanceTest.takePhoto();
 await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/location','/attendance/location','/attendance/photo']);
 assert.equal(f.c.attendanceTest.state().data.start.status,'STARTED');
 assert.equal(f.queued.length,0,'Recovered GPS must finish online rather than silently entering manual review');
}

{
 const f=fixture(async()=>{throw network();});
 await f.click('location');f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/location','/attendance/location']);
 assert.equal(f.queued.length,1,'A continuing outage keeps photo and GPS together in the offline queue');
 assert.equal(f.queued[0][0],'attendance');assert.equal(f.queued[0][2].purpose,'start');
 assert.equal(f.queued[0][2].latitude,44.9);assert.equal(await f.queued[0][3].text(),'camera');
 assert.equal(f.c.attendanceTest.state().data.start.status,'PENDING_REVIEW');
 assert.match(f.c.attendanceTest.state().html,/Начало ожидает синхронизации/);
 assert.match(f.c.attendanceTest.state().html,/сохранено на этом устройстве/);
 assert.match(f.c.attendanceTest.state().html,/Отправка фото и координат на сервер ещё не подтверждена/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Работа и обед доступны|data-att="lunch"/);
 assert.match(f.c.attendanceTest.state().html,/data-att="queue"/);
}

{
 const accepted={status:'PENDING_REVIEW',photoSaved:true,locationSaved:true,claimedAt:new Date().toISOString()};
 const f=fixture(async()=>({...base(),start:accepted}));
 // A stale unsent duplicate must not replace evidence the server already accepted.
 f.rows.push({kind:'attendance',date:base().date,status:'local',data:{purpose:'start',claimedAt:accepted.claimedAt}});
 await f.click('reload');
 assert.match(f.c.attendanceTest.state().html,/Начало смены записано на сервере/);
 assert.match(f.c.attendanceTest.state().html,/Работа и обед доступны/);
 assert.match(f.c.attendanceTest.state().html,/data-att="lunch"/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Начало ожидает синхронизации/);
}

{
 const f=fixture(async()=>({...base(),start:{status:'AWAITING_PHOTO',locationFresh:true,locationReceivedAt:new Date().toISOString()}}));
 await f.click('location');f.c.attendanceTest.takePhoto();
 // Keep a successfully acknowledged location: do not reset its server time on photo submission.
 await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/location','/attendance/photo']);
}

{
 const f=fixture();let stopped=0;
 const opening=f.c.attendanceTest.openCamera();
 assert.equal(f.c.attendanceTest.state().busy,true);
 assert.match(f.c.attendanceTest.state().html,/data-att="close" >Закрыть/);
 assert.match(f.c.attendanceTest.state().html,/data-att="camera-back" >Назад/);
 await f.click('camera-back');
 assert.equal(f.c.attendanceTest.state().busy,false);
 f.cameras.shift()({getTracks:()=>[{stop(){stopped++;}}]});await opening;
 assert.equal(stopped,1,'A camera granted after cancellation must be stopped immediately');
 assert.equal(f.c.attendanceTest.state().cameraStream,null);
}

{
 const f=fixture();let stopped=0;
 const opening=f.c.attendanceTest.openCamera();
 f.c.document.hidden=true;f.events.get('visibilitychange')();
 f.cameras.shift()({getTracks:()=>[{stop(){stopped++;}}]});await opening;
 assert.equal(stopped,1,'A late camera stream must not stay active while the app is hidden');
 assert.equal(f.c.attendanceTest.state().busy,false);
 assert.equal(f.c.attendanceTest.state().cameraActive,false);
 assert.equal(f.c.attendanceTest.state().cameraStream,null);
}

{
 const f=fixture(async(path,options)=>{
  assert.equal(path,'/attendance/photo');
  const end=options.body.purpose==='end';
  return {...base(),gpsRequired:false,start:{status:'STARTED',at:new Date().toISOString()},end:end?{status:'FINISHED',at:new Date().toISOString()}:null};
 });
 f.c.attendanceTest.configure({gpsRequired:false});
 assert.match(f.c.attendanceTest.state().html,/Геолокация временно отключена/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="location"|data-att="camera" disabled/);
 await f.click('location');
 assert.equal(f.calls.length,0,'Temporary no-GPS mode must not request or save any coordinates');
 f.c.attendanceTest.takePhoto();
 await Promise.all([f.click('confirm'),f.click('confirm')]);
 assert.deepEqual(f.calls,['/attendance/photo'],'Repeated submit must send one fresh photo');
 assert.equal(f.sent[0].purpose,'start');
 assert.equal(f.c.attendanceTest.state().data.start.status,'STARTED');
 f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/photo','/attendance/photo']);
 assert.equal(f.sent[1].purpose,'end');
 assert.equal(f.c.attendanceTest.state().data.end.status,'FINISHED');
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Геолокации и фотографии сохранены/);
}

{
 const f=fixture(async(path,options)=>{
  assert.equal(path,'/attendance/photo');assert.equal(options.body.purpose,'start');
  return {...base(),gpsRequired:false,start:{status:'STARTED',at:new Date().toISOString()}};
 });
 f.c.attendanceTest.configure({gpsRequired:false,start:{status:'PENDING_REVIEW',photoSaved:true,locationSaved:true,claimedAt:new Date().toISOString(),evidenceSource:'online'}});
 assert.match(f.c.attendanceTest.state().html,/Подтвердить начало по свежему фото/);
 assert.equal(f.c.attendanceTest.state().purpose,'end','Existing pending start must offer an explicit retake action');
 await f.click('retake-start');
 assert.equal(f.c.attendanceTest.state().purpose,'start');
 assert.match(f.c.attendanceTest.state().html,/Новое фото начала смены/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="location"|data-att="camera" disabled/);
 f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/photo']);
 assert.equal(f.c.attendanceTest.state().data.start.status,'STARTED');
}

{
 const pending={status:'PENDING_REVIEW',photoSaved:true,locationSaved:true,claimedAt:new Date().toISOString(),evidenceSource:'online'};
 const purposes=[];
 const f=fixture(async(path,options)=>{
  assert.equal(path,'/attendance/photo');purposes.push(options.body.purpose);
  return {...base(),gpsRequired:false,start:{status:'STARTED',at:new Date().toISOString()},end:options.body.purpose==='end'?{status:'FINISHED',at:new Date().toISOString()}:pending};
 });
 f.c.attendanceTest.configure({gpsRequired:false,start:pending,end:pending});
 assert.match(f.c.attendanceTest.state().html,/data-att="retake-start"/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="retake-end"/);
 await f.click('retake-start');f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.match(f.c.attendanceTest.state().html,/data-att="retake-end"/);
 await f.click('retake-end');f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.deepEqual(purposes,['start','end'],'Two pending online marks must recover in start/end order');
}

{
 const f=fixture();
 f.c.attendanceTest.configure({gpsRequired:false,start:{status:'PENDING_REVIEW',photoSaved:true,locationSaved:false,claimedAt:new Date().toISOString(),evidenceSource:'offline'}});
 assert.match(f.c.attendanceTest.state().html,/Офлайн-отметку проверяет руководитель/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="retake-start"/);
 await f.click('retake-start');
 assert.equal(f.c.attendanceTest.state().purpose,'end');
 assert.equal(f.c.attendanceTest.state().retakePurpose,null);
 assert.equal(f.calls.length,0);
}

{
 const f=fixture();f.c.navigator.onLine=false;
 f.c.attendanceTest.configure({gpsRequired:false});f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.equal(f.calls.length,0);assert.equal(f.queued.length,1);
 assert.equal(f.queued[0][2].purpose,'start');
 for(const key of ['latitude','longitude','accuracy'])assert.equal(f.queued[0][2][key],null,'No-GPS offline evidence must not manufacture coordinates');
 assert.match(f.c.attendanceTest.state().html,/Начало ожидает синхронизации/);
 assert.match(f.c.attendanceTest.state().html,/Отправка фото на сервер ещё не подтверждена/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Работа и обед доступны|data-att="lunch"|data-att="retake-start"/);
}

{
 const f=fixture();f.c.attendanceTest.configure({gpsRequired:true});
 assert.match(f.c.attendanceTest.state().html,/data-att="camera" disabled/);
 assert.match(f.c.attendanceTest.state().html,/data-att="location"/);
 f.c.attendanceTest.configure({gpsRequired:false});
 f.c.attendanceTest.takePhoto(new Date(Date.now()-301000).toISOString());await f.click('confirm');
 assert.equal(f.calls.length,0);assert.equal(f.queued.length,0);
 assert.match(f.c.attendanceTest.state().failure,/свежее фото/);
}

{
 const policy={gpsRequired:true,geofenceRequired:false,geoPolicy:'gps_only'};
 const f=fixture(async(path,options)=>{
  if(path==='/attendance/location')return {...base(),...policy,start:{status:'AWAITING_PHOTO',locationFresh:true,locationSaved:true,locationReceivedAt:new Date().toISOString()}};
  assert.equal(path,'/attendance/photo');assert.equal(options.body.purpose,'start');
  return {...base(),...policy,start:{status:'STARTED',at:new Date().toISOString(),locationSaved:true}};
 });
 f.c.attendanceTest.configure(policy);
 assert.match(f.c.attendanceTest.state().html,/Геолокация обязательна/);
 assert.match(f.c.attendanceTest.state().html,/Проверка расстояния до отеля временно отключена/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Геолокация временно отключена/);
 assert.match(f.c.attendanceTest.state().html,/data-att="camera" disabled/);
 await f.click('location');
 assert.match(f.c.attendanceTest.state().html,/Координаты сохранены на сервере/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="camera" disabled/);
 f.c.attendanceTest.takePhoto();
 assert.match(f.c.attendanceTest.state().html,/Подтвердить начало смены/);
 assert.doesNotMatch(f.c.attendanceTest.state().html,/Сохранить для проверки|Внутри настроенной зоны отеля/);
 await Promise.all([f.click('confirm'),f.click('confirm')]);
 assert.deepEqual(f.calls,['/attendance/location','/attendance/photo']);
 assert.equal(f.sent[0].latitude,44.9);assert.equal(f.sent[0].longitude,37.3);
 assert.equal(f.c.attendanceTest.state().data.start.status,'STARTED');
}

{
 const policy={gpsRequired:true,geofenceRequired:false,geoPolicy:'gps_only'};
 const pending={status:'PENDING_REVIEW',photoSaved:true,locationSaved:true,claimedAt:new Date().toISOString(),evidenceSource:'online',locationFresh:true,locationReceivedAt:new Date().toISOString()};
 const f=fixture(async(path,options)=>{
  if(path==='/attendance/location')return {...base(),...policy,start:pending};
  assert.equal(path,'/attendance/photo');assert.equal(options.body.purpose,'start');
  return {...base(),...policy,start:{status:'STARTED',at:new Date().toISOString(),locationSaved:true}};
 });
 f.c.attendanceTest.configure({...policy,start:pending});
 assert.match(f.c.attendanceTest.state().html,/Повторно подтвердить начало/);
 await f.click('retake-start');
 assert.equal(f.c.attendanceTest.state().purpose,'start');
 assert.match(f.c.attendanceTest.state().html,/data-att="camera" disabled/,'A previous fresh server coordinate must not bypass a newly requested GPS confirmation');
 await f.click('location');
 assert.equal(f.c.attendanceTest.state().purpose,'start','A pending location response must preserve the chosen start-recovery purpose');
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="camera" disabled/);
 f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.deepEqual(f.calls,['/attendance/location','/attendance/photo']);
 assert.equal(f.c.attendanceTest.state().data.start.status,'STARTED');
}

{
 const f=fixture();f.c.navigator.onLine=false;
 f.c.attendanceTest.configure({gpsRequired:true,geofenceRequired:false});
 f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.equal(f.queued.length,0,'GPS-only policy still requires actual fresh coordinates in offline evidence');
 await f.click('location');f.c.attendanceTest.takePhoto();await f.click('confirm');
 assert.equal(f.queued.length,1);assert.equal(f.queued[0][2].latitude,44.9);
 assert.equal(f.queued[0][2].longitude,37.3);
 assert.match(f.c.attendanceTest.state().html,/Начало ожидает синхронизации/);
}

{
 const f=fixture();f.c.attendanceTest.configure({canReview:true,eligible:true});
 assert.match(f.c.attendanceTest.state().html,/data-att="review"/,'A leader with a worker role needs access to team review too');
 await f.click('review');assert.equal(f.c.location.hash,'attendance');
 assert.equal(f.c.attendanceTest.state().open,false);
}

{
 const f=fixture();f.c.attendanceTest.configure({canReview:true,eligible:false});
 assert.match(f.c.attendanceTest.state().html,/Открыть контроль смен/);
 await f.click('review');assert.equal(f.c.location.hash,'attendance');
}

{
 const f=fixture();f.c.attendanceTest.configure({canReview:false});
 assert.doesNotMatch(f.c.attendanceTest.state().html,/data-att="review"/);
 await f.click('review');assert.equal(f.c.location.hash,'');
}

console.log('PASS: mandatory GPS with deferred hotel zones, strict mode, pending recovery, offline evidence, camera cleanup and leadership access');
