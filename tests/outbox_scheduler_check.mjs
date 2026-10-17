import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(setImmediate);};
function fixture(api){
 const rows=new Map(),calls=[],handlers={},timers=[];
 let now=100000,seq=0;
 const context=vm.createContext({rows,Blob,FormData,Promise,Event,Error,Date:{now:()=>now},
  crypto:{randomUUID:()=>`new-${++seq}`},navigator:{onLine:true},
  window:{dispatchEvent(){},addEventListener:(event,handler)=>{handlers[event]=handler;}},
  document:{hidden:false,addEventListener:(event,handler)=>{handlers[event]=handler;}},
  setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},
  localAction:async(_store,_mode,fn)=>fn({get:key=>rows.get(key),put:r=>rows.set(r.key,r),add:r=>rows.set(r.key,r)}),
  api:async(path,{body})=>{const op=body instanceof FormData?JSON.parse(body.get('operation')):body;calls.push(op.key);return api(op,path);}});
 vm.runInContext(source,context);
 vm.runInContext(`userId=42;outboxRows=async()=>[...rows.values()].map(r=>({...r,hasBlob:!!r.blob,blob:null}));`,context);
 const add=(key,extra={})=>{rows.set(key,{key,userId:42,kind:'shoot_photo',date:'2026-10-17',data:{shooting:{id:7}},
  filename:`${key}.jpg`,blob:new Blob(['original'],{type:'image/jpeg'}),status:'local',createdAt:1,order:rows.size,...extra});};
 return {rows,calls,handlers,timers,context,add,setNow:value=>{now=value;},run:code=>vm.runInContext(code,context)};
}

// A rate-limited receipt is not a global queue lock, including later passes.
{
 const f=fixture(async()=>({ok:true}));
 f.add('receipt',{kind:'sale_receipt',retryAt:161000,errorStatus:429});f.add('independent-photo');
 const result=await f.run('flushOutbox()');
 assert.equal(result.completed,1);assert.deepEqual(f.calls,['independent-photo']);
 assert.equal(f.rows.get('receipt').status,'local');assert.ok(f.rows.get('receipt').blob);
}
// One server failure or denied old operation must not strand other bookings.
{
 const f=fixture(async op=>{if(op.key==='old-denied'){const e=new Error('Нет доступа');e.status=403;throw e;}if(op.key==='bad-storage'){const e=new Error('Хранилище занято');e.status=503;throw e;}return {ok:true};});
 f.add('old-denied');f.add('bad-storage');f.add('independent-photo');await f.run('flushOutbox()');
 assert.equal(f.rows.get('old-denied').status,'error');assert.equal(f.rows.get('bad-storage').status,'local');
 assert.equal(f.rows.get('independent-photo').status,'synced');
}
// A timeout can occur just for Telegram receipt forwarding, not all media.
{
 const f=fixture(async op=>{if(op.kind==='sale_receipt')throw new Error('Таймаут чека');return {ok:true};});
 f.add('receipt',{kind:'sale_receipt'});f.add('independent-photo');await f.run('flushOutbox()');
 assert.equal(f.rows.get('independent-photo').status,'synced');assert.ok(f.rows.get('receipt').blob);
}
// Later retry windows must never cancel the earliest pending retry.
{
 const f=fixture(async()=>({ok:true}));f.add('earlier',{retryAt:130000});f.add('later',{retryAt:190000});
 await f.run('flushOutbox()');assert.deepEqual(f.timers.map(t=>t.delay),[30000]);
}
// The explicit Sync button retries a lost response now, keeping key and bytes,
// without bypassing server Retry-After or reviving permanently denied actions.
{
 let lost=true;const f=fixture(async()=>{if(lost)throw new Error('Response lost');return {ok:true};});
 f.add('lost-response');f.add('rate-limited',{retryAt:161000,errorStatus:429});
 f.add('access-denied',{status:'error',errorStatus:403});
 await f.run('flushOutbox()');assert.equal(f.rows.get('lost-response').retryAt,130000);
 assert.equal(await f.rows.get('lost-response').blob.text(),'original');
 lost=false;await f.run('syncOutboxNow()');
 assert.deepEqual(f.calls,['lost-response','lost-response']);
 assert.equal(f.rows.get('lost-response').status,'synced');
 assert.equal(f.rows.get('rate-limited').retryAt,161000);assert.ok(f.rows.get('rate-limited').blob);
 assert.equal(f.rows.get('access-denied').status,'error');
}
// A file added while an earlier request is in flight must get its own pass.
{
 let finishFirst;const first=new Promise(resolve=>{finishFirst=resolve;});
 const f=fixture(async op=>op.key==='first'?first:{ok:true});f.add('first');
 const flushing=f.run('flushOutbox()');await settle();
 await f.run(`enqueueBatch([{kind:'shoot_photo',date:'2026-10-17',data:{shooting:{id:7}},blob:new Blob(['next'],{type:'image/jpeg'})}])`);
 finishFirst({ok:true});await flushing;await settle();
 assert.deepEqual(f.calls,['first','new-1']);assert.equal(f.rows.get('new-1').status,'synced');
}
// Only a newly confirmed login recovers a 401. Genuine access denial stays put.
{
 const f=fixture(async()=>({ok:true}));
 f.add('expired',{status:'error',errorStatus:401,error:'Войдите снова'});
 f.add('forbidden',{status:'error',errorStatus:403,error:'Нет доступа'});
 await f.run('flushOutbox()');assert.deepEqual(f.calls,[]);
 await f.run('setOutboxUser(42)');
 assert.deepEqual(f.calls,['expired']);assert.equal(f.rows.get('expired').status,'synced');
 assert.equal(f.rows.get('forbidden').status,'error');
}
// A fresh 401 cannot be retried endlessly by foreground events.
{
 const f=fixture(async()=>{const e=new Error('Войдите снова');e.status=401;throw e;});f.add('expired');
 await f.run('setOutboxUser(42)');await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['expired']);
 f.setNow(100001);await f.run('setOutboxUser(42)');assert.deepEqual(f.calls,['expired','expired']);
}
// Foregrounding wakes a queue whose background timer the browser suspended.
{
 const f=fixture(async()=>({ok:true}));f.add('resumed');
 assert.equal(typeof f.handlers.visibilitychange,'function');f.handlers.pageshow();await settle();
 assert.equal(f.rows.get('resumed').status,'synced');
}
// Definitively rejected bytes may be corrected in place; uncertain sends may not.
{
 const f=fixture(async()=>({ok:true}));f.add('bad-photo',{status:'error',errorStatus:413});
 f.context.replacement=Object.assign(new Blob(['corrected'],{type:'image/jpeg'}),{name:'fixed.jpg'});
 await f.run('replaceOperationFile("bad-photo",replacement)');
 assert.deepEqual(f.calls,['bad-photo']);assert.equal(f.rows.size,1);assert.equal(f.rows.get('bad-photo').filename,'fixed.jpg');
 f.add('uncertain',{status:'local',errorStatus:503});
 await assert.rejects(f.run('replaceOperationFile("uncertain",replacement)'),/нельзя заменять/);
 assert.equal(await f.rows.get('uncertain').blob.text(),'original');
 f.add('camera-attendance',{kind:'attendance',status:'error',errorStatus:413});
 await assert.rejects(f.run('replaceOperationFile("camera-attendance",replacement)'),/нельзя заменять/);
 assert.equal(await f.rows.get('camera-attendance').blob.text(),'original','attendance must retain the camera-only original');
}
console.log('PASS: backoff isolates rows, in-flight additions flush, renewed auth recovers 401, foreground resumes uploads');
