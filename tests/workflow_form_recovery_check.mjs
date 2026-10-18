import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/workflow.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const rows=[];let seq=0,resets=0,manualSync=0,automaticSync=0;
class FD{constructor(form){this.form=form;}get(name){return this.form.values[name];}}
const form={id:'workflowSale',dataset:{},classList:{contains:()=>false},values:{total:'150',sold:'1'},elements:{selected:{files:[{type:'image/jpeg',size:1234,name:'one.jpg'}]},receipt:{files:[{type:'image/jpeg',size:1234,name:'receipt.jpg'}]}},reset(){resets++;}};
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
