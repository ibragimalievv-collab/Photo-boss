import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','') + '\nasync function writeOperations(items){for(const row of items)await localAction(\"operations\",\"readwrite\",store=>store.put(row));return true;}';
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
// Sale steps must wait for the receipt and preceding photos in their own chain.
// A transient receipt failure used to send counts/photos early, making their
// wrong-stage 409 errors permanent even after the receipt recovered.
{
 let receiptReady=false,countsReady=false,selectedReady=false;
 const f=fixture(async op=>{
  if(op.kind==='sale_receipt'&&!receiptReady){const e=new Error('Хранилище занято');e.status=503;throw e;}
  if(op.kind==='sale_counts'){
   if(!receiptReady){const e=new Error('Черновик продажи уже закрыт.');e.status=409;throw e;}
   countsReady=true;
  }
  if(op.kind==='sale_selected'){
   if(!countsReady){const e=new Error('Сначала сохраните чек и количество кадров.');e.status=409;throw e;}
   selectedReady=true;
  }
  if(op.kind==='sale_complete'&&!selectedReady){const e=new Error('Продажа уже завершена или отменена.');e.status=409;throw e;}
  return {ok:true};
 });
 f.add('start',{kind:'sale_start',blob:null,data:{booking:{id:7}},status:'synced'});
 const draft={key:'start'};
 f.add('receipt',{kind:'sale_receipt',data:{draft}});
 f.add('counts',{kind:'sale_counts',blob:null,data:{draft,total:2,sold:1}});
 f.add('selected',{kind:'sale_selected',data:{draft}});
 f.add('complete',{kind:'sale_complete',blob:null,data:{draft}});
 f.add('independent-photo');
 await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['receipt','independent-photo']);
 for(const key of ['counts','selected','complete'])assert.equal(f.rows.get(key).status,'local');
 assert.ok(f.rows.get('selected').blob,'waiting selected photo must retain its original');
 await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['receipt','independent-photo'],'a receipt cooldown must also hold its sale steps');
 receiptReady=true;f.setNow(130001);await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['receipt','independent-photo','receipt','counts','selected','complete']);
 assert.ok([...f.rows.values()].every(row=>row.status==='synced'));
}
// A permanent receipt error waits for correction without discarding later photos.
{
 const f=fixture(async()=>({ok:true}));
 f.add('rejected-receipt',{kind:'sale_receipt',status:'error',errorStatus:413,data:{draft:{key:'draft'}}});
 f.add('waiting-photo',{kind:'sale_selected',data:{draft:{key:'draft'}}});
 f.add('independent-photo');await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['independent-photo']);
 assert.equal(f.rows.get('waiting-photo').status,'local');assert.ok(f.rows.get('waiting-photo').blob);
}
// Already saved queues from the previous release recover only their exact
// wrong-stage errors, after the original predecessor has succeeded.
{
 const f=fixture(async()=>({ok:true})),draft={key:'start'};
 f.add('start',{kind:'sale_start',blob:null,status:'synced'});
 f.add('receipt',{kind:'sale_receipt',data:{draft},status:'synced'});
 f.add('counts',{kind:'sale_counts',blob:null,data:{draft,total:2,sold:1},status:'error',errorStatus:409,error:'Черновик продажи уже закрыт.'});
 f.add('selected',{kind:'sale_selected',data:{draft},status:'error',errorStatus:409,error:'Сначала сохраните чек и количество кадров.'});
 f.add('complete',{kind:'sale_complete',blob:null,data:{draft},status:'error',errorStatus:409,error:'Продажа уже завершена или отменена.'});
 f.add('denied',{kind:'sale_counts',blob:null,data:{draft:{key:'other'}},status:'error',errorStatus:403,error:'Нет доступа'});
 await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['counts','selected','complete']);
 for(const key of ['counts','selected','complete']){
  assert.equal(f.rows.get(key).status,'synced');
  assert.equal(f.rows.get(key).saleDependencyRecovery,'sale-dependency-recovery-v1-20261017');
 }
 assert.equal(f.rows.get('denied').status,'error');
}
// A genuine closed sale is rechecked at most once. Financial/duplicate conflicts
// and an unproven chain never qualify for automatic legacy recovery.
{
 const f=fixture(async()=>{const e=new Error('Черновик продажи уже закрыт.');e.status=409;throw e;}),draft={key:'closed'};
 f.add('closed',{kind:'sale_start',status:'synced',blob:null});
 f.add('receipt',{kind:'sale_receipt',data:{draft},status:'synced'});
 f.add('closed-counts',{kind:'sale_counts',blob:null,data:{draft},status:'error',errorStatus:409,error:'Черновик продажи уже закрыт.'});
 f.add('price-change',{kind:'sale_counts',blob:null,data:{draft:{key:'price'}},status:'error',errorStatus:409,error:'Цена пакета изменилась. Обновите данные и проверьте продажу.'});
 f.add('duplicate',{kind:'sale_selected',data:{draft:{key:'duplicate'}},status:'error',errorStatus:409,error:'Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.'});
 f.add('unproven',{kind:'sale_complete',blob:null,data:{draft:{key:'missing-start'}},status:'error',errorStatus:409,error:'Продажа уже завершена или отменена.'});
 await f.run('flushOutbox()');await f.run('flushOutbox()');
 assert.deepEqual(f.calls,['closed-counts']);
 for(const key of ['closed-counts','price-change','duplicate','unproven'])assert.equal(f.rows.get(key).status,'error');
}
// Later retry windows must never cancel the earliest pending retry.
{
 const f=fixture(async op=>op.kind==='sale_complete'?{saleId:9,amount:'400.00',soldPhotos:1}:{draftId:3});
 const draft={key:'start'},keys=['start','receipt','counts','photo','complete'];
 f.add('start',{kind:'sale_start',status:'synced',blob:null,data:{booking:{id:7}},result:{draftId:3}});
 f.add('receipt',{kind:'sale_receipt',status:'synced',blob:null,data:{draft}});
 f.add('counts',{kind:'sale_counts',status:'synced',blob:null,data:{draft,total:150,sold:1,price:'400.00'}});
 f.add('photo',{kind:'sale_selected',status:'synced',blob:null,data:{draft},result:{photoId:11}});
 f.add('complete',{kind:'sale_complete',status:'error',blob:null,data:{draft,price:'400.00'},errorStatus:409,error:'Полная съёмка содержит 1 кадров. Общее количество менять нельзя.'});
 const accepted=keys.slice(0,4).map(key=>JSON.stringify(f.rows.get(key))),payload=JSON.stringify(f.rows.get('complete').data);
 await f.run('repairSaleQueue(["start","receipt","counts","photo","complete"],{draftId:3,total:1,sold:1,price:"400.00"})');await settle();
 assert.deepEqual(f.calls,['new-1','complete'],'corrected counts must precede retrying original completion');
 assert.equal(f.rows.size,6);assert.equal(f.rows.get('complete').status,'synced');
 assert.equal(JSON.stringify(f.rows.get('complete').data),payload,'original idempotent request payload stays unchanged');
 for(let i=0;i<4;i++)assert.equal(JSON.stringify(f.rows.get(keys[i])),accepted[i],'accepted receipt/count/photo rows stay unchanged');
 assert.equal(f.rows.get('new-1').data.total,1);assert.equal(f.rows.get('counts').data.total,150);
}
// A reload after accepting a correction still resumes the original operation.
{
 const f=fixture(async()=>({saleId:10,amount:'400.00'})),draft={key:'start'};
 f.add('complete',{kind:'sale_complete',status:'error',blob:null,data:{draft},errorStatus:409,error:'Сначала загрузите все выбранные фотографии: 1/2.',resumeByKey:'repair'});
 f.add('repair',{kind:'sale_selected',status:'synced',blob:null,data:{draft:{id:3}},resumeCompleteKey:'complete'});
 await f.run('flushOutbox()');await settle();
 assert.deepEqual(f.calls,['complete']);assert.equal(f.rows.get('complete').status,'synced');
}
// Uncertain completion and files that are still pending cannot be unlocked.
{
 const f=fixture(async op=>{if(op.kind==='sale_complete'){const error=new Error('Сначала загрузите все выбранные фотографии: 1/2.');error.status=409;throw error;}return {draftId:3};}),draft={key:'start'};
 f.add('start',{kind:'sale_start',status:'synced',blob:null,result:{draftId:3}});
 f.add('counts',{kind:'sale_counts',status:'synced',blob:null,data:{draft,total:2,sold:2}});
 f.add('photo',{kind:'sale_selected',status:'synced',blob:null,data:{draft}});
 f.add('complete',{kind:'sale_complete',status:'error',blob:null,data:{draft},errorStatus:409,error:'Сначала загрузите все выбранные фотографии: 1/2.'});
 await f.run('repairSaleQueue(["start","counts","photo","complete"],{draftId:3,total:2,sold:2,price:"400.00"})');await settle();
 await f.run('flushOutbox()');await settle();
 assert.deepEqual(f.calls,['new-1','complete'],'an unsuccessful explicit repair must not create an automatic retry loop');
 assert.equal(f.rows.get('complete').status,'error');
}
// Uncertain completion and files that are still pending cannot be unlocked.
{
 const f=fixture(async()=>({})),draft={key:'start'};
 f.add('start',{kind:'sale_start',status:'synced',blob:null,result:{draftId:3}});
 f.add('complete',{kind:'sale_complete',status:'local',blob:null,data:{draft},errorStatus:503,error:'Timeout'});
 await assert.rejects(f.run('repairSaleQueue(["start","complete"],{draftId:3,total:1,sold:1,price:"400.00"})'),/Исправление недоступно/);
 assert.equal(f.rows.size,2);assert.equal(f.calls.length,0);
 f.rows.get('complete').status='error';f.rows.get('complete').errorStatus=409;f.rows.get('complete').error='Сначала загрузите все выбранные фотографии: 0/1.';
 f.add('pending',{kind:'sale_selected',status:'local',data:{draft}});
 await assert.rejects(f.run('repairSaleQueue(["start","pending","complete"],{draftId:3,total:1,sold:1,price:"400.00"})'),/Исправление недоступно/);
 assert.ok(f.rows.get('pending').blob);assert.equal(f.rows.size,3);
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
 f.add('duplicate-photo',{kind:'sale_selected',status:'error',errorStatus:409,error:'Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.'});
 await f.run('replaceOperationFile("duplicate-photo",replacement)');
 assert.equal(f.rows.get('duplicate-photo').status,'synced');assert.equal(f.rows.get('duplicate-photo').filename,'fixed.jpg');
 assert.equal(f.calls.at(-1),'duplicate-photo','corrected photo must keep the rejected operation key');
 for(const [key,status,message] of [['denied',403,'Нет доступа'],['price',409,'Цена пакета изменилась. Обновите данные и проверьте продажу.']]){
  f.add(key,{kind:'sale_selected',status:'error',errorStatus:status,error:message});
  await assert.rejects(f.run(`replaceOperationFile('${key}',replacement)`),/нельзя заменять/);
  assert.equal(await f.rows.get(key).blob.text(),'original');
 }
}
console.log('PASS: backoff isolates rows, in-flight additions flush, renewed auth recovers 401, foreground resumes uploads');

// Old durable packets cannot be relabelled with the new reset generation.
{
 const packets=[];const f=fixture(async op=>{packets.push(op);return {ok:true};});
 f.add('legacy',{blob:null});f.add('old-generation',{blob:null,generation:'before'});f.add('new-generation',{blob:null,generation:'after'});
 await f.run('setOutboxUser(42,"after")');
 assert.deepEqual(f.calls,['new-generation']);assert.equal(packets[0].generation,'after');
 assert.equal(f.rows.get('legacy').status,'local');assert.equal(f.rows.get('old-generation').status,'local');
 await f.run('enqueueBatch([{key:"fresh-reset-operation",kind:"booking",date:"2026-10-17",data:{}}])');await settle();
 assert.equal(f.rows.get('fresh-reset-operation').generation,'after');
 assert.ok(packets.every(op=>op.generation==='after'));
}
// A result returning after a reset may not recreate a purged local row.
{
 let finish;const f=fixture(()=>new Promise(resolve=>{finish=resolve;}));
 f.add('in-flight',{blob:null});const flushing=f.run('flushOutbox()');await settle();
 f.rows.clear();f.handlers['pb-operations-reset']({detail:{userId:42,generation:'after'}});
 finish({ok:true});await flushing;await settle();assert.equal(f.rows.size,0);
}
console.log('PASS: durable reset generations and stale in-flight completion cannot resurrect old operations');
// Expenses are outside the authorized reset; preserve legacy request payloads.
{
 const packets=[];const f=fixture(async op=>{packets.push(op);return {ok:true};});
 f.add('retained-expense',{kind:'cash_expense',blob:null,data:{note:'retained'}});
 await f.run('setOutboxUser(42,"after")');
 assert.deepEqual(f.calls,['retained-expense']);
 assert.equal(Object.hasOwn(packets[0],'generation'),false,'legacy expense hash must remain unchanged');
}
console.log('PASS: retained cash expense replays its exact legacy packet after operational reset');
