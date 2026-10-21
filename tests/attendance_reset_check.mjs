import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const original=fs.readFileSync(new URL('../app/attendance_ui/attendance.js',import.meta.url),'utf8');
const source=original.replace(/^import .*;\n/gm,'').replaceAll('export ','').replace('refresh();card();',`
 globalThis.attendanceTest={refresh,openCamera,
  prime(){generation=7;data={eligible:true};photo='old-photo';point={latitude:1};pointAt=123;pointSaved=true;photoAt='old-time';busy=true;cameraActive=true;dialog.open=true;cameraStream={getTracks:()=>[{stop(){globalThis.cameraStops++;}}]};},
  state(){return {generation,data,photo,point,pointAt,pointSaved,photoAt,busy,cameraActive,cameraStream,open:dialog.open};}
 };
`);

function fixture(){
 const events=new Map(),requests=[],cameras=[];
 const dialog={open:false,setAttribute(){},addEventListener(){},querySelector(){return null;},close(){this.open=false;},showModal(){this.open=true;}};
 const root={querySelector(){return null;},prepend(){}};
 const document={querySelector:s=>s==='#app'?root:null,createElement:tag=>tag==='dialog'?dialog:{},head:{append(){}},body:{append(){}},addEventListener(){}};
 const c=vm.createContext({console,Date,Number,Intl,Promise,Error,setTimeout,clearTimeout,document,location:{hash:''},
  window:{addEventListener:(name,handler)=>events.set(name,handler)},
  navigator:{mediaDevices:{getUserMedia:()=>new Promise(resolve=>cameras.push(resolve))}},
  MutationObserver:class{observe(){}},api:()=>new Promise(resolve=>requests.push(resolve)),
  outboxRows:async()=>[],esc:x=>x,requestAnimationFrame(){},cameraStops:0});
 vm.runInContext(source,c);
 return {c,events,requests,cameras};
}

{
 const {c,events,requests}=fixture();
 c.attendanceTest.prime();
 const inFlight=c.attendanceTest.refresh();
 events.get('pb-operations-reset')();
 let state=c.attendanceTest.state();
 assert.equal(state.generation,8);assert.equal(state.data,null);assert.equal(state.photo,null);
 assert.equal(state.point,null);assert.equal(state.pointAt,0);assert.equal(state.photoAt,null);
 assert.equal(state.pointSaved,false);
 assert.equal(state.busy,false);assert.equal(state.cameraActive,false);assert.equal(state.cameraStream,null);
 assert.equal(state.open,false);assert.equal(c.cameraStops,1);
 requests.shift()({eligible:true,start:{status:'STARTED'},end:{status:'FINISHED'}});
 await inFlight;
 assert.equal(c.attendanceTest.state().data,null,'An old attendance response must not refill cleared state');
 const refreshed=c.attendanceTest.refresh(),fresh={eligible:false,start:null,end:null};
 requests.shift()(fresh);await refreshed;
 assert.equal(c.attendanceTest.state().data,fresh);
}

{
 const {c,events,cameras}=fixture();
 c.attendanceTest.prime();
 const pending=c.attendanceTest.openCamera();
 events.get('pb-operations-reset')();
 cameras.shift()({getTracks:()=>[{stop(){c.cameraStops++;}}]});
 await pending;
 assert.equal(c.cameraStops,2,'Both the old camera and late pre-reset stream must stop');
 assert.equal(c.attendanceTest.state().cameraStream,null);
 assert.equal(c.attendanceTest.state().busy,false);
}

console.log('PASS: reset clears attendance photo/GPS/camera, drops stale status and stops late camera streams');
