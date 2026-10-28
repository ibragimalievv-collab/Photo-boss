import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/outbox.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const settle=async()=>{for(let i=0;i<12;i++)await new Promise(setImmediate);};
function fixture(respond){
 const rows=new Map(),calls=[],timers=[],handlers={};let clock=100000;
 const context=vm.createContext({rows,Blob,FormData,Promise,Event,Error,structuredClone,Date:{now:()=>clock},Number,JSON,
  crypto:{randomUUID:()=> 'new-operation-key-0001'},navigator:{onLine:true},
  window:{dispatchEvent(){},addEventListener:(event,fn)=>{handlers[event]=fn;}},
  document:{hidden:false,addEventListener:(event,fn)=>{handlers[event]=fn;}},
  setTimeout(fn,delay){timers.push({fn,delay});return timers.length;},clearTimeout(){},
  writeOperations:async updates=>{for(const row of updates)rows.set(row.key,structuredClone(row));return true;},
  localAction:async(_store,_mode,fn)=>fn({get:key=>structuredClone(rows.get(key)),put:row=>rows.set(row.key,structuredClone(row))}),
  api:async(path,options)=>{const body=options.body instanceof FormData?JSON.parse(options.body.get('operation')):options.body;calls.push({path,body:structuredClone(body)});return respond(path,body);}});
 vm.runInContext(source,context);
 vm.runInContext("userId=42;outboxRows=async()=>[...rows.values()].map(row=>({...structuredClone(row),hasBlob:!!row.blob,blob:null}));",context);
 const add=(key,extra={})=>rows.set(key,{key,userId:42,generation:'',salesGeneration:'',kind:'shoot_photo',date:'2026-10-28',data:{},status:'local',createdAt:1,order:rows.size,blob:null,...extra});
 const sale=()=>{
  add('sale-start-key-0001',{kind:'sale_start',status:'synced',data:{booking:{id:7}},result:{draftId:3}});
  const draft={key:'sale-start-key-0001'};
  add('sale-bank-key-0001',{kind:'sale_bank_payment',data:{draft,booking:{id:7},payment:{total:2,sold:1,tariffId:1,discountId:null,albumId:null,albumQuantity:null}}});
  add('sale-counts-key-0001',{kind:'sale_counts',data:{draft,total:2,sold:1,price:'400.00'}});
  add('selected-key-0001',{kind:'sale_selected',data:{draft},blob:new Blob(['original'],{type:'image/jpeg'}),filename:'selected.jpg'});
  add('sale-complete-key-0001',{kind:'sale_complete',data:{draft,price:'400.00'}});
 };
 return {rows,calls,timers,handlers,context,add,sale,setNow:now=>{clock=now;},run:code=>vm.runInContext(code,context)};
}
const pending={id:12,bookingId:7,amount:'400.00',status:'PENDING',testMode:false,qrPath:'/bank-payments/12/qr'};
// Booking must first be acknowledged. QR cannot be created from a pending or
// rejected booking, and no payment request is needed while rendering a form.
{
 const f=fixture(async(path,body)=>path==='/operations/sync'?{bookingId:7}:pending);
 f.add('booking-key-0001',{kind:'booking',data:{deposit:'1000'}});
 f.add('booking-bank-key-0001',{kind:'booking_bank_payment',data:{booking:{key:'booking-key-0001'},payment:{}}});
 await f.run('flushOutbox()');await settle();
 assert.equal(f.calls.filter(call=>call.path==='/bank-payments').length,1);
 const create=f.calls.find(call=>call.path==='/bank-payments');
 assert.equal(create.body.bookingId,7);assert.equal(create.body.purpose,'DEPOSIT');assert.equal(create.body.requestKey,'booking-bank-key-0001');
 assert.equal(f.rows.get('booking-bank-key-0001').result.id,12);
}
// A new sale durably retains its originals and never sends counts or completes
// before a real paid result. Unrelated uploads keep moving while payment waits.
{
 let paid=false;
 const f=fixture(async path=>path==='/bank-payments'?pending:path.endsWith('/status')?{...pending,status:paid?'PAID':'PENDING'}:{ok:true});f.sale();
 f.add('independent-original',{kind:'shoot_photo',data:{shooting:{id:9}},blob:new Blob(['other'])});
 await f.run('flushOutbox()');await settle();
 assert.ok(f.calls.some(call=>call.body?.key==='independent-original'));
 assert.ok(!f.calls.some(call=>call.body?.kind?.startsWith('sale_')));
 assert.equal(f.rows.get('sale-bank-key-0001').status,'local');
 assert.equal(await f.rows.get('selected-key-0001').blob.text(),'original');
 assert.equal(f.rows.get('sale-counts-key-0001').status,'local');
 const savedPayload=JSON.stringify(f.rows.get('sale-bank-key-0001').bankPayload);
 paid=true;await f.run('syncOutboxNow()');await settle();
 assert.equal(f.rows.get('sale-bank-key-0001').status,'synced');
 assert.equal(f.rows.get('sale-complete-key-0001').status,'synced');
 assert.equal(f.calls.filter(call=>call.path==='/bank-payments').length,1);
 assert.equal(f.calls.filter(call=>call.path.endsWith('/status')).length,1);
 assert.equal(JSON.stringify(f.rows.get('sale-bank-key-0001').bankPayload),savedPayload);
 await f.run('flushOutbox()');assert.equal(f.calls.filter(call=>call.body?.kind==='sale_complete').length,1);
}
// A timeout after registration reuses both durable request key and exact body;
// neither another invoice nor an unpaid sale may be created as a fallback.
{
 let fail=true;const attempts=[];
 const f=fixture(async(path,body)=>{if(path==='/bank-payments'){attempts.push(JSON.stringify(body));if(fail)throw Error('Lost registration response');return pending;}return {ok:true};});f.sale();
 await f.run('flushOutbox()');await settle();
 assert.equal(f.rows.get('sale-bank-key-0001').status,'local');assert.ok(f.rows.get('selected-key-0001').blob);
 fail=false;await f.run('syncOutboxNow()');await settle();
 assert.equal(attempts.length,2);assert.equal(attempts[0],attempts[1]);
 assert.equal(f.rows.get('sale-complete-key-0001').status,'local');
}
// Sandbox, refunded, cancelled, or ambiguous paid statuses never release files.
for(const bill of [{...pending,status:'TEST_PAID',testMode:true},{...pending,status:'PAID',testMode:true},{...pending,status:'PAID',testMode:undefined},{...pending,status:'CANCELLED'},{...pending,status:'PARTIAL_REFUND'},{...pending,status:'REFUNDED'}]){
 const f=fixture(async()=>bill);f.sale();await f.run('flushOutbox()');await settle();
 assert.equal(f.rows.get('sale-bank-key-0001').status,'error');
 assert.equal(f.rows.get('sale-counts-key-0001').status,'local');assert.ok(f.rows.get('selected-key-0001').blob);
 assert.equal(f.calls.filter(call=>call.path==='/operations/sync'||call.path==='/operations/media').length,0);
}
// Only the explicit locked server zero-due proof can bypass a new payment.
{
 const f=fixture(async path=>path==='/bank-payments'?{allowZeroDue:true,bookingId:7,sourceDraftId:3,amount:'0.00',status:'NO_PAYMENT_REQUIRED',testMode:false}:{ok:true});f.sale();
 await f.run('flushOutbox()');await settle();assert.equal(f.rows.get('sale-complete-key-0001').status,'synced');
 assert.equal(f.calls[0].body.allowZeroDue,true);assert.equal(f.calls[0].body.sourceDraftId,3);
}
for(const proof of [{allowZeroDue:true,bookingId:8,sourceDraftId:3,amount:'0.00',status:'NO_PAYMENT_REQUIRED',testMode:false},{allowZeroDue:false,bookingId:7,sourceDraftId:3,amount:'0.00',status:'NO_PAYMENT_REQUIRED',testMode:false}]){
 const f=fixture(async()=>proof);f.sale();await f.run('flushOutbox()');await settle();assert.equal(f.rows.get('sale-bank-key-0001').status,'error');assert.ok(f.rows.get('selected-key-0001').blob);
}
// A prior sales reset and a changed account cannot create or acknowledge a bill.
{
 const f=fixture(async()=>pending);f.sale();vm.runInContext("salesGeneration='new-sales-reset'",f.context);
 await f.run('flushOutbox()');await settle();assert.equal(f.calls.length,0);assert.ok(f.rows.get('selected-key-0001').blob);
}
{
 let resolve;const answer=new Promise(done=>{resolve=done;});const f=fixture(async()=>answer);f.sale();
 const flushing=f.run('flushOutbox()');await settle();vm.runInContext('userId=null',f.context);resolve(pending);await flushing;await settle();
 assert.equal(f.rows.get('sale-bank-key-0001').result,undefined);assert.ok(f.rows.get('selected-key-0001').blob);
}
console.log('PASS: automatic booking/sale QR gates are durable, retain files, reuse uncertain requests, and require real bank payment or exact zero-due proof');
