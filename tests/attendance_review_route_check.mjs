import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/team.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const requests=[],root={innerHTML:''};
let state={actorId:3,geofenceRequired:false,scopeNotice:'Только назначенные отели',items:[
 {id:17,user_id:1,name:'Фотограф',shift_date:'2026-10-21',purpose:'start',confirmedAt:null,offline_claimed_at:'2026-10-21T06:10:00+00:00',location_received_at:'2026-10-21T06:09:00+00:00',latitude:44.9,longitude:37.3,geofence:{hotelName:'Отель',notice:'Расстояние пока не проверялось.'}},
 {id:18,user_id:3,name:'Владелец',shift_date:'2026-10-21',purpose:'start',confirmedAt:null,offline_claimed_at:null,location_received_at:null,latitude:null,longitude:null,geofence:null}
]};
const context=vm.createContext({Date,Number,console,location:{hash:'#attendance'},document:{querySelector:()=>root},
 esc:value=>value??'',money:value=>String(value),ROLE_NAMES:{},FormData:class{constructor(form){this.form=form;}[Symbol.iterator](){return Object.entries(this.form.values)[Symbol.iterator]();}},
 api:async(path,options)=>{
  requests.push([path,options]);
  if(path==='/hr')throw new Error('HR unavailable');
  if(path==='/attendance/pending')return structuredClone(state);
  assert.equal(path,'/attendance/review');
  assert.equal(options.body.userId,1);
  state.items[0].confirmedAt=options.body.verifiedAt;
  state.items[0].status='STARTED';
  return {ok:true};
 }});
vm.runInContext(source,context);
await context.loadAttendanceControls();
root.innerHTML=context.attendanceControlsView();
assert.equal(requests.length,1);
assert.equal(requests[0][0],'/attendance/pending');
assert.match(root.innerHTML,/Контроль смен/);
assert.match(root.innerHTML,/Только назначенные отели/);
assert.match(root.innerHTML,/Расстояние до отеля пока не проверяется/);
assert.match(root.innerHTML,/data-user="1"/);
assert.doesNotMatch(root.innerHTML,/data-user="3"/,'The owner must not receive a self-approval form');
assert.match(root.innerHTML,/другой руководитель/);
assert.match(root.innerHTML,/Время не сохранено/);
assert.doesNotMatch(root.innerHTML,/1970|Invalid Date/);
const form={dataset:{user:'1',day:'2026-10-21',purpose:'start'},id:'',classList:{contains:name=>name==='attendance-review'},
 values:{approved:'true',verifiedAt:'2026-10-21T09:10',note:'Фото и время проверены'}};
await context.teamSubmit(form);
assert.deepEqual(requests.map(([path])=>path),['/attendance/pending','/attendance/review','/attendance/pending']);
assert.match(root.innerHTML,/Подтверждено/);
assert.match(root.innerHTML,/data-expected-at=/);
assert.equal(context.location.hash,'#attendance');
assert.doesNotMatch(root.innerHTML,/Кандидаты|Команда и найм/,'A review must keep the leader in attendance controls without loading HR');
state.items[0].confirmedAt='2026-10-21T06:10:37+00:00';
await context.loadAttendanceControls();
root.innerHTML=context.attendanceControlsView();
const timestamp=new Date(state.items[0].confirmedAt);
const localTimestamp=new Date(timestamp.getTime()-timestamp.getTimezoneOffset()*60000).toISOString().slice(0,19);
assert.ok(root.innerHTML.includes(`step="1" name="verifiedAt" value="${localTimestamp}"`),'Reopening a confirmed timestamp must preserve its local date and seconds');
console.log('PASS: independent leadership route, self-approval hidden, missing times honest, decision reload stays in controls');
