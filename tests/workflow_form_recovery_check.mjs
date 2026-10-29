import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/workflow.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const rows=[];let seq=0,resets=0,manualSync=0,automaticSync=0;
class FD{constructor(form){this.form=form;}get(name){return this.form.values[name];}}
const form={setAttribute(){},removeAttribute(){},id:'workflowSale',dataset:{draftScope:'7'},classList:{contains:()=>false},values:{total:'150',sold:'1'},elements:{selected:{files:[{type:'image/jpeg',size:1234,name:'one.jpg'}]},receipt:{files:[{type:'image/jpeg',size:1234,name:'receipt.jpg'}]}},reset(){resets++;}};
const c=vm.createContext({FormData:FD,Date,Number,JSON,Error,Set,form,rows,checkSaleFiles:async()=>{},captureForm(){},captureForms(){},restoreForms(){},clearForm(){},todayKey:()=> '2026-10-16',crypto:{randomUUID:()=>`test-key-${++seq}`},window:{addEventListener(){}},document:{addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},enqueueBatch:async batch=>{rows.push(...batch.map(x=>({...x,status:'local'})));},outboxRows:async()=>rows,retryOperation:async()=>{},flushOutbox:async()=>{automaticSync++;return {state:'offline'};},syncOutboxNow:async()=>{manualSync++;return {state:'done'};}});
vm.runInContext(source,c);vm.runInContext("data={actorId:42,bookings:[{id:7,price:'400.00',draft:null}]};selected=7",c);
await vm.runInContext('workflowSubmit(form)',c);assert.equal(resets,0);assert.equal(rows.length,5);assert.equal(JSON.parse(form.dataset.pendingKeys).length,5);
await vm.runInContext('workflowSubmit(form)',c);assert.equal(rows.length,5,'second press must reuse saved operations');assert.equal(resets,0);
rows.forEach(r=>r.status='synced');await assert.rejects(vm.runInContext('workflowSubmit(form)',c),/уже сохранена/);
const beforeSync=automaticSync;
await vm.runInContext(`workflowClick({dataset:{workflow:'sync'}})`,c);
assert.equal(manualSync,1,'Sync now must invoke the explicit manual cooldown override');assert.equal(automaticSync,beforeSync);
const progress=vm.runInContext(`saleTransferProgress([
 {kind:'sale_start',status:'synced',result:{draftId:2}},
 {kind:'sale_receipt',status:'synced'},
 {kind:'sale_selected',status:'synced',result:{photoId:1,selectedPhotos:1}},
 {kind:'sale_selected',status:'synced',result:{photoId:1,duplicate:true,selectedPhotos:1}},
 {kind:'sale_selected',status:'local'},
 {kind:'sale_selected',status:'syncing'},
 {kind:'sale_selected',status:'error'}])`,c);
assert.match(progress,/разных кадров: 1/);assert.match(progress,/Файлов принято 2 из 5/);
assert.match(progress,/повторных копий: 1/);assert.match(progress,/Ожидают отправки: 1; передаются: 1; ошибки файлов: 1/);
const legacyProgress=vm.runInContext(`saleTransferProgress([{kind:'sale_selected',status:'synced',result:{photoId:7}},{kind:'sale_selected',status:'synced',result:{photoId:7,duplicate:true}}])`,c);
assert.match(legacyProgress,/В этой отправке подтверждено разных кадров: 1/,'older successful operations without progress fields still distinguish duplicate copies');
console.log('PASS: sale fields do not reset, receipt/photos are queued once, repeat uses the same operations');

assert.equal(vm.runInContext("bookingAccepted([{kind:'booking',status:'synced',result:{bookingId:8}},{kind:'booking_receipt',status:'error'}])",c),true,'A pending receipt must not block the next saved booking');
assert.equal(vm.runInContext("bookingAccepted([{kind:'booking',status:'local'}])",c),false,'Do not clear a booking before server acceptance');

form.elements.selected.files=[{type:'',size:1234,name:'camera-original.JPG'}];
assert.equal(vm.runInContext("imageFiles(form,'selected').length",c),1,'Some device file providers omit MIME for valid JPEG originals');
form.elements.selected.files=[{type:'',size:1234,name:'unsupported.heic'}];
assert.throws(()=>vm.runInContext("imageFiles(form,'selected')",c),/JPEG, PNG или WebP/);
form.elements.selected.files=[{type:'application/pdf',size:1234,name:'renamed.jpg'}];
assert.throws(()=>vm.runInContext("imageFiles(form,'selected')",c),/JPEG, PNG или WebP/,'A known incompatible MIME cannot be bypassed by renaming');
form.elements.selected.files=[{type:'',size:21*1024*1024,name:'too-large.jpg'}];
assert.throws(()=>vm.runInContext("imageFiles(form,'selected')",c),/не больше 20 МБ/);
console.log('PASS: device originals without MIME are accepted by extension, unsupported types and oversized files stay rejected');

let repairedBooking=null;
c.repairBookingQueue=async(keys,value)=>{repairedBooking={keys:[...keys],value};};
rows.length=0;rows.push({key:'rejected-booking-key',kind:'booking',status:'error',errorStatus:409});
form.id='workflowBooking';form.dataset={pendingKeys:JSON.stringify(['rejected-booking-key']),repairingBooking:'1'};
form.values={client_name:'Гость',client_phone:'  +79991234567  ',hotel_id:'2',package_id:'1',room:'101',guest_count:'2',deposit:'1000',shoot_date:'2026-10-17',shoot_time:'12:00'};
await vm.runInContext('workflowSubmit(form)',c);
assert.deepEqual(repairedBooking.keys,['rejected-booking-key']);
assert.equal(repairedBooking.value.client_phone,'+79991234567');assert.equal(repairedBooking.value.hotel_id,2);
assert.equal(rows.length,1,'Correcting a rejected booking must not enqueue a second booking or duplicate receipt');
assert.equal(form.dataset.repairingBooking,undefined);
console.log('PASS: correcting booking fields reuses the rejected operation and trims optional phone input');

const controls=['client_name','client_phone','hotel_id','package_id','room','guest_count','deposit','shoot_date','shoot_time'].map(name=>({name,type:'text'}));
controls.push({name:'receipt',type:'file'});for(const control of controls)controls[control.name]=control;
const submit={type:'submit',disabled:false},parts=new Map();
const bookingForm={id:'workflowBooking',dataset:{pendingKeys:'["rejected-booking-key"]'},elements:controls,
 querySelector(selector){if(selector==='button[type="submit"],button:not([type])')return submit;return parts.get(selector)||null;},
 append(node){if('operationProgress' in node.dataset)parts.set('[data-operation-progress]',node);if(node.dataset.workflow)parts.set(`[data-workflow="${node.dataset.workflow}"]`,node);}};
c.bookingForm=bookingForm;c.fixtureRows=[{key:'rejected-booking-key',kind:'booking',status:'error',errorStatus:409,error:'Проверьте данные гостя, дату, количество гостей и сумму брони.'}];
c.rejectedBooking=rows=>rows[0]?.status==='error'?rows[0]:null;
c.document.querySelectorAll=()=>[bookingForm];c.document.createElement=()=>({dataset:{},setAttribute(){}});
vm.runInContext('queue=fixtureRows;updateFormProgress()',c);
assert.ok(controls.every(control=>control.disabled));assert.match(parts.get('[data-operation-progress]').textContent,/Запись не была создана/);
assert.equal(parts.get('[data-workflow="repair-booking"]').hidden,false);
bookingForm.dataset.repairingBooking='1';vm.runInContext('updateFormProgress()',c);
assert.ok(controls.filter(control=>control.type!=='file').every(control=>!control.disabled));
assert.equal(controls.receipt.disabled,true,'The original queued receipt remains untouched during field repair');
assert.equal(submit.textContent,'Сохранить исправленную запись');
assert.equal(parts.get('[data-workflow="repair-booking"]').hidden,true);
console.log('PASS: rejected booking shows an edit action, re-enables its data fields, and preserves the queued receipt');

// Receipt preflight uses the same real quote as the visible total: confirmed
// money is allocated across previous sales, packages, discounts and albums.
const pricingSource=fs.readFileSync(new URL('../app/webapp/js/hotel-pricing.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const balanceCases=[
 {name:'partial deposit',paid:'400',sold:3,total:10,due:800},
 {name:'fully covered purchase',paid:'1200',sold:3,total:10,due:0},
 {name:'credit already used by previous purchase',paid:'1000',previous:'800',sold:1,total:10,due:200},
 {name:'remaining approved credit',paid:'1200',previous:'800',sold:1,total:10,due:0},
 {name:'package purchase',paid:'400',sold:3,total:10,tariff:'7',due:800},
 {name:'whole shoot approved discount',paid:'400',sold:3,total:3,discount:'9',due:200},
 {name:'discount plus two albums',paid:'400',sold:3,total:3,discount:'9',album:'5',albumQuantity:'2',due:1200},
 {name:'fully covered discount and albums',paid:'1600',sold:3,total:3,discount:'9',album:'5',albumQuantity:'2',due:0},
 {name:'existing draft still needs receipt',paid:'400',sold:3,total:10,draft:{status:'AWAITING_RECEIPT'},due:800},
 {name:'unknown quote cannot bypass receipt',paid:'99999',sold:3,total:10,tariff:'999',due:null},
];
for(const example of balanceCases){
 const queued=[],files=Array.from({length:example.sold},(_,i)=>({name:`original-${i}.jpg`,type:'image/jpeg',size:1234}));
 const values={sold:String(example.sold),total:String(example.total),tariffId:example.tariff||'1',discountId:example.discount||'',albumId:example.album||'',albumQuantity:example.albumQuantity||'1',paymentMethod:'receipt'};
 const candidate={id:'workflowSale',isConnected:true,dataset:{draftScope:'7'},classList:{contains:()=>false},setAttribute(){},removeAttribute(){},querySelector:()=>null,
  values,elements:{...Object.fromEntries(Object.entries(values).map(([key,value])=>[key,{value}])),selected:{files},receipt:{files:[]}}};
 const booking={id:7,hotelId:1,price:'400.00',pricing:{tariffId:1,kind:'UNIT',price:'400.00'},paidAmount:example.paid,salesAmount:example.previous||'0',draft:example.draft||null,
  discountApprovals:[{id:9,status:'APPROVED',total:3,unitPrice:'400.00'}]};
 const catalogue={actorId:42,bookings:[booking],tariffs:[{id:7,hotelId:1,kind:'PACKAGE',price:'1200.00',photoCount:3}],albums:[{id:5,hotelId:1,price:'500.00'}]};
 let key=0;
 const ctx=vm.createContext({FormData:FD,Date,Number,JSON,Error,Set,candidate,catalogue,checkSaleFiles:async()=>{},captureForm(){},todayKey:()=> '2026-10-29',
  crypto:{randomUUID:()=>`balance-${++key}`},window:{addEventListener(){}},document:{addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
  enqueueBatch:async batch=>queued.push(...batch),outboxRows:async()=>queued});
 vm.runInContext(pricingSource+'\n'+source,ctx);
 vm.runInContext('data=catalogue;state=catalogue;selected=7;',ctx);
 const quote=vm.runInContext('updatePricePreview(candidate,data.bookings[0],data.tariffs)',ctx);
 assert.equal(quote?.due??null,example.due,example.name);
 if(example.due===0){
  await vm.runInContext('workflowSubmit(candidate)',ctx);
  assert.ok(queued.some(row=>row.kind==='sale_complete'),example.name);
  assert.ok(!queued.some(row=>row.kind==='sale_receipt'),'fully covered quote needs no new receipt');
 }else{
  await assert.rejects(vm.runInContext('workflowSubmit(candidate)',ctx),example.due===null?/Проверьте сумму продажи/:/Прикрепите чек продажи на остаток к оплате/);
  assert.equal(queued.length,0,example.name+' must fail before enqueue');
  assert.equal(candidate.elements.selected.files,files,'preflight preserves chosen originals');
  assert.equal(candidate.dataset.pendingKeys,undefined);
 }
}
console.log('PASS: receipt preflight uses real outstanding balance across prior purchases, approved credit, packages, discounts and albums; full coverage alone permits no new receipt');
