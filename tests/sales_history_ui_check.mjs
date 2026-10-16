import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/workflow.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const ctx=vm.createContext({window:{addEventListener(){}},document:{addEventListener(){}},navigator:{onLine:true},Set,Date,Intl,Number,JSON,
 esc:x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;'),todayKey:()=> '2026-10-16'});
vm.runInContext(source,ctx);
vm.runInContext(`data={canBook:false,bookings:[{id:7,client:'Guest <unsafe>',status:'COMPLETED',price:'400.00',date:'2026-10-15',room:'100',fullUploaded:true,totalFrames:10,soldTotal:2,salesAmount:'800.00',paidAmount:'800.00',dueAmount:'0.00',sales:[{id:1,at:'2026-10-15T10:00:00Z',photos:2,amount:'800.00',paymentStatus:'PAID'}]}]};selected=7;queue=[];`,ctx);
const markup=vm.runInContext('workflowView()',ctx);
assert.ok(markup.includes('История покупок'));
assert.ok(markup.includes('Дополнительно продано кадров'));
assert.ok(markup.includes('Новый чек доплаты'));
assert.ok(markup.includes('Добавить допродажу'));
assert.ok(markup.includes('Редактировать / добавить покупку'));
assert.ok(markup.includes('Предыдущие продажи и чеки не перезаписываются'));
assert.ok(markup.includes('value="10" readonly'));
assert.ok(markup.includes('Guest &lt;unsafe>'));
assert.ok(markup.includes('value="7" selected'));
console.log('PASS: closed-order repeat purchase, history totals, locked frame count and escaped names');
