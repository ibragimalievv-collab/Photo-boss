import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','') + '\nasync function writeOperations(items){for(const row of items)await localAction(\"operations\",\"readwrite\",store=>store.put(row));return true;}';
const rows=new Map(),calls=[],timers=[];
let now=100000,mode='rate';
const photo=new Blob(['original bytes'],{type:'image/jpeg'});
const row=(key,extra={})=>({key,userId:42,kind:'shoot_photo',date:'2026-10-16',data:{shooting:{id:7}},
 filename:key+'.jpg',blob:photo,status:'local',createdAt:1,order:rows.size,...extra});
rows.set('original-key',row('original-key'));
const context=vm.createContext({rows,Blob,FormData,Promise,Event,Error,Date:{now:()=>now},
 navigator:{onLine:true},window:{dispatchEvent(){},addEventListener(){}},
 document:{hidden:false,addEventListener(){}},
 setTimeout(_fn,delay){timers.push(delay);return timers.length;},clearTimeout(){},
 localAction:async(_store,_mode,fn)=>fn({get:key=>rows.get(key),put:r=>rows.set(r.key,r)}),
 api:async(_path,{body})=>{
  const op=JSON.parse(body.get('operation'));calls.push(op.key);
  assert.equal(await body.get('file').text(),'original bytes');
  if(mode==='rate'){const e=new Error('Slow down');e.status=429;e.retryAfterMs=61000;throw e;}
  if(mode==='invalid'){const e=new Error('Не удалось прочитать вложение. Повторите отправку.');e.status=400;throw e;}
  return {ok:true,photoId:9};
 }});
vm.runInContext(source,context);
vm.runInContext(`userId=42;outboxRows=async()=>[...rows.values()].map(r=>({...r,hasBlob:!!r.blob,blob:null}));`,context);
let result=await vm.runInContext('flushOutbox()',context);
assert.equal(result.temporary,1);assert.equal(rows.get('original-key').status,'local');
assert.ok(rows.get('original-key').blob);assert.equal(rows.get('original-key').retryAt,161000);
assert.ok(timers.at(-1)>=61000);
await vm.runInContext('retryOperation("original-key")',context);
assert.equal(calls.length,1,'manual retry must respect Retry-After');
now=162000;mode='ok';
await vm.runInContext('flushOutbox()',context);
assert.equal(rows.get('original-key').status,'synced');assert.equal(rows.get('original-key').blob,null);
assert.deepEqual(calls,['original-key','original-key'],'replay must preserve operation identity');
rows.clear();calls.length=0;mode='invalid';
rows.set('legacy',row('legacy',{status:'error',errorStatus:400,error:'Не удалось прочитать вложение. Повторите отправку; если ошибка повторится, выберите файл заново.'}));
rows.set('previous-release',row('previous-release',{status:'error',errorStatus:400,legacyMediaRecovered:true,error:'Не удалось прочитать вложение. Повторите отправку.'}));
rows.set('unrelated',row('unrelated',{status:'error',errorStatus:400,error:'Неверное поле'}));
rows.set('forbidden',row('forbidden',{status:'error',errorStatus:403,error:'Нет доступа'}));
await vm.runInContext('flushOutbox()',context);
await vm.runInContext('flushOutbox()',context);
assert.deepEqual(calls,['legacy','previous-release'],'legacy media failure including last release recovery may be rechecked, once per fix');
assert.ok(rows.get('legacy').legacyMediaRecovered);assert.ok(rows.get('legacy').blob);
assert.equal(rows.get('previous-release').legacyMediaRecovery,'media-recovery-v2-20261017');assert.ok(rows.get('previous-release').blob);
console.log('PASS: 429 honors Retry-After, preserves original/key, and legacy false-400 errors recover once without retrying access denials');

context.navigator.onLine=false;rows.clear();
const bookingError='Проверьте данные гостя, дату, количество гостей и сумму брони.';
const rejected={key:'booking-original-key',userId:42,kind:'booking',date:'2026-10-16',data:{client_phone:'123'},status:'error',errorStatus:409,error:bookingError,createdAt:1};
const receipt={key:'booking-receipt-key',userId:42,kind:'booking_receipt',date:'2026-10-16',data:{booking:{key:rejected.key},purpose:'DEPOSIT'},status:'local',errorStatus:424,error:'Сначала синхронизируйте предыдущую операцию этой записи.',blob:photo,createdAt:2};
rows.set(rejected.key,rejected);rows.set(receipt.key,receipt);
const originalReceiptData=JSON.stringify(receipt.data),originalReceiptBytes=await receipt.blob.text();
context.correctedBooking={client_name:'Гость',client_phone:'+79991234567',hotel_id:1,package_id:1,room:'101',guest_count:2,deposit:'1000',shoot_date:'2026-10-17',shoot_time:'12:00',photographer_id:null};
assert.ok(vm.runInContext('rejectedBooking([...rows.values()])',context));
await vm.runInContext('repairBookingQueue([...rows.keys()],correctedBooking)',context);
assert.equal(rows.size,2);assert.equal(rows.get(rejected.key).key,'booking-original-key');
assert.equal(rows.get(rejected.key).data.client_phone,'+79991234567');assert.equal(rows.get(rejected.key).status,'local');
assert.equal(JSON.stringify(rows.get(receipt.key).data),originalReceiptData);
assert.equal(await rows.get(receipt.key).blob.text(),originalReceiptBytes);assert.equal(rows.get(receipt.key).errorStatus,0);
assert.equal(calls.length,2,'Repair must not send while offline');
for(const changes of [{status:'synced',result:{bookingId:9}},{status:'local',errorStatus:0},
 {errorStatus:503},{errorStatus:401},{error:'Ключ уже использован для других данных.'},
 {error:'Продажи обнулены владельцем. Обновите приложение и начните продажу заново; старые файлы сохранены.'}]){
 context.candidate={...rejected,status:'error',errorStatus:409,error:bookingError,...changes};
 assert.equal(vm.runInContext('rejectedBooking([candidate])',context),null,'Only a definitive rolled-back booking validation can be changed');
}
rows.set(rejected.key,{...rejected,status:'error',errorStatus:409,error:bookingError,generation:'old'});
await assert.rejects(vm.runInContext('repairBookingQueue([...rows.keys()],correctedBooking)',context),/Исправление записи недоступно/);
console.log('PASS: rejected bookings can be corrected under their original key; receipt bytes/dependency survive; uncertain, accepted, auth and reset operations cannot be changed');
