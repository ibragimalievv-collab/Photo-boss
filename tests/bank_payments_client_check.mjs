import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/bank-payments.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const listeners={},calls=[],status={textContent:''},button={disabled:false},files=Array.from({length:20},(_,index)=>({name:`frame-${index}.jpg`}));
const sale={dataset:{pendingKeys:'["sale-start","sale-photo20"]'},elements:{total:{value:'150'},sold:{value:'20'},selected:{files}}};
const bankForm={id:'bankPaymentForm',dataset:{},values:{purpose:'PURCHASE',amount:''},querySelector:selector=>selector==='button'?button:status};
const panel={};bankForm.closest=()=>panel;
const booking={id:7,hotelId:1,sales:[{id:11}],draft:null,paidAmount:'1000',salesAmount:'8000'};
const queued=[{key:'sale-start',status:'synced',result:{draftId:9}},{key:'sale-photo20',status:'synced',result:{saleId:11,draftId:9}}];
let quoteCalls=0;
const context=vm.createContext({Number,JSON,Error,Set,rows:queued,sale,b:booking,settingsFixture:{tariffs:[],bankConfigured:true},
 FormData:class{constructor(form){this.form=form;}get(key){return this.form.values[key];}},
 crypto:{randomUUID:()=> 'bank-request-key-20'},
 esc:value=>String(value??''),URL:{revokeObjectURL(){}},
 updatePricePreview(){quoteCalls++;return {amount:8000,due:7000,tariffId:1};},
 outboxRows:async()=>queued,
 api:async(path,options)=>{calls.push({path,options});if(path.startsWith('/workflow'))return {bookings:[booking],tariffs:[]};const error=new Error('Сеть временно недоступна.');error.status=503;throw error;},
 window:{addEventListener(){}},
 document:{addEventListener:(name,fn)=>{listeners[name]=fn;},querySelector:selector=>selector==='#workflowSale'?sale:selector==='#bankPanel'?panel:null},
});
vm.runInContext(source,context);
let payload=vm.runInContext("bankPaymentPayload(sale,b,settingsFixture,{purpose:'PURCHASE',amount:'',requestKey:'test-key'},rows)",context);
assert.equal(payload.purpose,'BALANCE');assert.equal(payload.sourceDraftId,9);assert.equal(payload.sold,0);
assert.equal(quoteCalls,0,'A completed displayed purchase must not be priced again');
queued[1].status='local';delete queued[1].result;
payload=vm.runInContext("bankPaymentPayload(sale,b,settingsFixture,{purpose:'PURCHASE',amount:'',requestKey:'test-key'},rows)",context);
assert.equal(payload.purpose,'PURCHASE');assert.equal(payload.sourceDraftId,9);assert.equal(payload.sold,20);
assert.equal(quoteCalls,1,'An in-flight purchase carries its draft identity for the server race check');
for(const purpose of ['BALANCE','DEPOSIT']){
 context.paymentPurpose=purpose;
 payload=vm.runInContext("bankPaymentPayload(null,b,settingsFixture,{purpose:paymentPurpose,amount:'',requestKey:'test-key'},rows)",context);
 assert.equal(payload.purpose,purpose);assert.equal(payload.sold,0);assert.equal(payload.total,0);
}
assert.equal(quoteCalls,1,'Paying a deposit or existing balance cannot require photo counts');
queued[1]={key:'sale-photo20',status:'synced',result:{saleId:11,draftId:9}};
vm.runInContext('bankPanel(b,settingsFixture)',context);
const event={target:bankForm,preventDefault(){}};
await listeners.submit(event);
assert.equal(calls[1].options.body.purpose,'BALANCE');
assert.equal(status.textContent,'Сеть временно недоступна.');assert.equal(button.disabled,false);
assert.equal(sale.elements.selected.files,files);assert.equal(files.length,20);assert.equal(sale.elements.sold.value,'20');
const originalPayload=calls[1].options.body;
bankForm.values.amount='500';await listeners.submit(event);
assert.equal(calls[3].options.body,originalPayload,'An uncertain bank registration retries its exact original payload/key');
assert.equal(calls[3].options.body.amount,null);
console.log('PASS: twenty-photo invoice uses completed balance once, in-flight draft identity survives, deposit/balance need no photo fields, and bank failures preserve20files and exact retry payload');
