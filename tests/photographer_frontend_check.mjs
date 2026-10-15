import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {File} from 'node:buffer';

const source=path=>fs.readFileSync(new URL(`../${path}`,import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const plain=value=>JSON.parse(JSON.stringify(value));

// Exercise actual selection/submission paths with browser File metadata missing.
const sent=[],queued=[],workflowNodes={'#workflowQueue':{innerHTML:''},'#workflowFolderProgress':{isConnected:true,textContent:''}};
const workflow=vm.createContext({File,Blob,Date,URL,Error,Number,Promise,
 FormData:class{constructor(form){this.values=form.values;}get(key){return this.values[key]??null;}},
 crypto:{randomUUID:()=>String(Math.random())},navigator:{onLine:true},
 document:{hidden:false,querySelector:selector=>workflowNodes[selector]||null,addEventListener(){}},
 window:{addEventListener(){}},setTimeout:()=>0,clearTimeout(){},
 esc:String,todayKey:()=> '2026-10-15',api:async()=>({bookings:[]}),outboxRows:async()=>queued,
 enqueueBatch:async items=>sent.push(...items),enqueueFilesOneByOne:async files=>sent.push(...files),
});
vm.runInContext(source('app/webapp/js/workflow.js'),workflow);
const unnamed=new File(['actual JPEG bytes'],'portrait.JPG',{type:''});
workflow.testFile=unnamed;
const normalized=vm.runInContext('normalizeImageFile(testFile)',workflow);
assert.equal(normalized.type,'image/jpeg');assert.equal(normalized.name,'portrait.JPG');assert.equal(await normalized.text(),await unnamed.text());
workflow.testFile=new File(['pdf'],'portrait.jpg',{type:'application/pdf'});
assert.equal(vm.runInContext('normalizeImageFile(testFile)',workflow),null,'an explicit incompatible MIME must not be silently rewritten');
vm.runInContext("data={bookings:[{id:1,shootingId:7,canUpload:true,status:'READY_FOR_SALE',files:[{status:'STORED'}]}]}",workflow);
const form={id:'workflowShoot',values:{booking:'1',finish:'on'},elements:{photos:{files:[]}},classList:{contains:()=>false},reset(){}};
workflow.testForm=form;
queued.push({kind:'shoot_photo',data:{shooting:{id:7}},status:'error'});
await assert.rejects(vm.runInContext('workflowSubmit(testForm)',workflow),/исправьте ошибки/);
assert.equal(sent.length,0);
queued.length=0;
vm.runInContext("data.bookings[0].files=[{status:'FAILED'}]",workflow);
await assert.rejects(vm.runInContext('workflowSubmit(testForm)',workflow),/каждый кадр сохранён/);
vm.runInContext("data.bookings[0].files=[{status:'STORED'}]",workflow);
await vm.runInContext('workflowSubmit(testForm)',workflow);
assert.equal(sent[0].kind,'shoot_complete');
sent.length=0;
workflowNodes['#workflowShoot']={elements:{booking:{value:'1'}}};
workflow.folderInput={files:[unnamed,new File(['text'],'readme.txt')],disabled:false,value:'folder'};
await vm.runInContext('workflowFolderChange(folderInput)',workflow);
assert.equal(sent.length,1);assert.equal(sent[0].type,'image/jpeg');
assert.equal(workflow.folderInput.disabled,false);

// A locally checked item is immediately visible, then adopts confirmed server state.
const workdayQueue=[{kind:'checklist',date:'2026-10-15',status:'local',data:{itemKey:'camera',done:true}}];
let serverChecked=false;
const workday=vm.createContext({Date,Intl,crypto:{randomUUID:()=> 'id'},window:{addEventListener(){}},setTimeout:()=>0,clearTimeout(){},
 api:async()=>({date:new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date()),timezone:'Europe/Moscow',items:[{key:'camera',done:serverChecked,title:'Подготовить камеру'}],sales:[],closed:false}),
 outboxRows:async()=>workdayQueue,esc:String,money:String,ROLE_NAMES:{},
});
// Keep this test independent from the calendar date of the test runner.
workdayQueue[0].date=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
vm.runInContext(source('app/webapp/js/workday.js'),workday);
await vm.runInContext('loadWorkday()',workday);
assert.match(vm.runInContext('workdayView()',workday),/выполнено · ожидает синхронизации/);
workdayQueue[0].status='synced';serverChecked=true;
await vm.runInContext('loadWorkday()',workday);
assert.doesNotMatch(vm.runInContext('workdayView()',workday),/ожидает синхронизации/);
assert.match(vm.runInContext('workdayView()',workday),/выполнено/);

// Exercise real attendance UI actions: HTTP failure must never say coordinates
// were saved, and photo confirmation must enter review rather than bypass location.
async function attendanceScenario({locationFails=false,aiRejected=false}={}){
 const handlers={},dialogHandlers={},queued=[],calls=[];let dialog;
 const video={videoWidth:640,videoHeight:480,play:async()=>{}};
 const errorNode={hidden:true,textContent:''};
 const element=()=>({children:[],setAttribute(){},addEventListener(name,fn){dialogHandlers[name]=fn;},querySelector(selector){return selector==='#attCameraVideo'?video:errorNode;},append(){},remove(){},open:false,showModal(){this.open=true;},close(){this.open=false;}});
 const root={querySelector:()=>null,prepend(){}};
 const base={eligible:true,date:new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date()),timezone:'Europe/Moscow',shiftId:11,startTime:'11:00',lateFine:500,start:null,end:null,shifts:[{id:11,hotelName:'Test',startAt:'2026-10-15T08:00:00Z',endAt:'2026-10-15T17:00:00Z'}]};
 const document={hidden:false,querySelector(selector){return selector==='#app'?root:selector==='#topbar'?{children:[{}]}:null;},head:{append(){}},body:{append(el){dialog=el;}},addEventListener(name,fn){handlers[name]=fn;},createElement(tag){if(tag==='canvas')return {getContext:()=>({fillRect(){},drawImage(){}}),toDataURL:()=> 'data:image/jpeg;base64,YQ=='};return element();}};
 class CustomEvent extends Event{constructor(name,options){super(name);this.detail=options?.detail;}}
 const context=vm.createContext({document,window:{addEventListener(){},dispatchEvent(){}},location:{hash:'#home'},
 navigator:{onLine:true,geolocation:{getCurrentPosition(ok){ok({coords:{latitude:43,longitude:39,accuracy:8}});}},mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop(){}}]})}},
 MutationObserver:class{observe(){}},Event,CustomEvent,Date,Intl,Error,Promise,Blob,Uint8Array,atob,setTimeout,clearTimeout,requestAnimationFrame:fn=>fn(),esc:String,
 outboxRows:async()=>queued,enqueueOperation:async(kind,date,data,blob)=>queued.push({kind,date,data,blob,status:'local'}),
 api:async(path,options)=>{calls.push({path,body:options?.body});if(!options)return structuredClone(base);
  assert.equal(options.body.shiftId,11,'every request must retain selected shift');
  if(path==='/attendance/location'){if(locationFails){const error=Error('temporarily unavailable');error.status=503;throw error;}return {...structuredClone(base),start:{status:'AWAITING_PHOTO',locationFresh:true,locationSaved:true}};}
  if(path==='/attendance/photo')return {...structuredClone(base),start:{status:aiRejected?'AWAITING_PHOTO':'STARTED',locationFresh:true,aiStatus:aiRejected?'REJECTED':'APPROVED',aiNote:aiRejected?'Ступни не видны':'Полный рост подтверждён'}};
  throw Error('unexpected path '+path);
 }});
 vm.runInContext(source('app/attendance_ui/attendance.js'),context);
 await new Promise(resolve=>setTimeout(resolve,0));
 const click=async action=>handlers.click({target:{closest:()=>({dataset:{att:action}})},preventDefault(){},stopImmediatePropagation(){}});
 await click('open');await click('location');
 if(locationFails){assert.match(dialog.innerHTML,/Сервер ещё не подтвердил/);assert.doesNotMatch(dialog.innerHTML,/✓ Координаты сохранены/);}
 await click('camera');await click('snap');await click('confirm');
 if(locationFails){assert.equal(queued.length,1);assert.equal(queued[0].data.shiftId,11);assert.equal(calls.some(c=>c.path==='/attendance/photo'),false);assert.match(dialog.innerHTML,/Начало ожидает проверки/);}
 if(aiRejected){assert.match(dialog.innerHTML,/Ступни не видны/);assert.match(dialog.innerHTML,/Начало ещё не подтверждено/);assert.doesNotMatch(dialog.innerHTML,/Вы на смене/);}
 return calls;
}
await attendanceScenario({locationFails:true});
await attendanceScenario({aiRejected:true});
console.log('PASS: MIME-less JPEG folder upload, Disk completion guard, shift-pinned attendance, honest network failure, and rejected AI photo retry');
