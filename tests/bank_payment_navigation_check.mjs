import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/bank-payments.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
async function waitForCalls(h,count){for(let i=0;i<20&&h.calls.length<count;i++)await Promise.resolve();assert.ok(h.calls.length>=count,'Expected bank request was not started');}
function harness(){
 const listeners={},windowListeners={},calls=[],created=[],revoked=[],events=[];
 let panel=null,respond=async()=>{throw new Error('Unexpected request');};
 const sale={dataset:{},elements:{total:{value:'10'},sold:{value:'2'}}};
 const context=vm.createContext({Number,JSON,Error,Set,
  esc:value=>String(value??''),crypto:{randomUUID:()=> 'invoice-request'},
  FormData:class{constructor(form){this.form=form;}get(key){return this.form.values[key];}},
  Event:class{constructor(type){this.type=type;}},
  URL:{createObjectURL(blob){created.push(blob);return 'blob:'+created.length;},revokeObjectURL(url){revoked.push(url);}},
  updatePricePreview:()=>({tariffId:1}),outboxRows:async()=>[],
  api:async(path,options)=>{calls.push({path,options});return respond(path,options);},
  window:{addEventListener:(name,fn)=>{windowListeners[name]=fn;},dispatchEvent:event=>events.push(event.type)},
  document:{addEventListener:(name,fn)=>{listeners[name]=fn;},
   querySelector:selector=>selector==='#bankPanel'?panel:selector==='#workflowSale'?sale:
    selector==='#bankQR'?panel?.qr:selector==='#bankBills'?panel?.bills:
    selector==='#bankPaymentForm'?panel?.form:selector==='#bankPaymentForm [role=status]'?panel?.message:null,
   createElement:()=>({style:{}})},
 });
 vm.runInContext(source,context);
 function mount(id){
  const qr={innerHTML:'',textContent:'',images:[],append(image){this.images.push(image);}};
  const bills={insertions:[],insertAdjacentHTML(_where,html){this.insertions.push(html);}};
  const message={textContent:''},submit={disabled:false},row={outerHTML:''};
  const mounted={qr,bills,message,row};
  const form={id:'bankPaymentForm',dataset:{},values:{purpose:'PURCHASE',amount:''},
   closest:()=>mounted,querySelector:selector=>selector==='button'?submit:message};
  mounted.form=form;mounted.submit=submit;
  mounted.querySelector=selector=>selector==='#bankQR'?qr:selector==='#bankBills'?bills:
   selector.includes('[role=status]')?message:null;
  mounted.action=(kind,billId=1)=>{
   const button={dataset:{[kind]:String(billId)},disabled:false};
   button.closest=selector=>selector==='#bankPanel'?mounted:selector==='[data-bank-bill]'?row:button;
   return button;
  };
  panel=mounted;context.fixture={id,sales:[],bankPayments:[]};
  vm.runInContext('bankPanel(fixture,{tariffs:[],bankConfigured:true})',context);
  return mounted;
 }
 return {mount,calls,created,revoked,events,respond:fn=>{respond=fn;},
  click:button=>listeners.click({target:button}),
  submit:form=>listeners.submit({target:form,preventDefault(){}}),windowListeners,listeners};
}

// A QR request starts on A; changing the selected sale must not attach it to B.
{
 const h=harness(),a=h.mount(7),answer=deferred();h.respond(()=>answer.promise);
 const first=h.click(a.action('bankQr')),b=h.mount(8);
 answer.resolve({qr:'A'});await first;
 assert.equal(h.created.length,0);assert.equal(b.qr.images.length,0);
}
// A late PAID response must not refresh B or announce A's payment on B.
{
 const h=harness(),a=h.mount(7),answer=deferred();h.respond(()=>answer.promise);
 const first=h.click(a.action('bankCheck')),b=h.mount(8);
 answer.resolve({id:1,status:'PAID'});await first;
 assert.equal(h.calls.length,1);assert.equal(b.qr.textContent,'');assert.deepEqual(h.events,[]);
}
// Two invoices in the same sale follow the most recent QR button, not response order.
{
 const h=harness(),a=h.mount(7),older=deferred(),newer=deferred();
 h.respond(path=>path.includes('/1/')?older.promise:newer.promise);
 const first=h.click(a.action('bankQr',1)),second=h.click(a.action('bankQr',2));
 newer.resolve({invoice:2});await second;older.resolve({invoice:1});await first;
 assert.equal(h.created.length,1);assert.equal(h.created[0].invoice,2);
}
// Switching after the status response but before its workflow refresh is also safe.
{
 const h=harness(),a=h.mount(7),answer=deferred();
 h.respond(path=>path.endsWith('/status')?{id:1,status:'PAID'}:answer.promise);
 const first=h.click(a.action('bankCheck'));
 await waitForCalls(h,2);
 assert.equal(h.calls[1].path,'/workflow?booking=7');const b=h.mount(8);
 answer.resolve({bookings:[{id:7}],tariffs:[]});await first;
 assert.equal(b.qr.textContent,'');assert.deepEqual(h.events,[]);
}
// A delayed preflight must never register an invoice using the newly selected sale.
{
 const h=harness(),a=h.mount(7),answer=deferred();h.respond(()=>answer.promise);
 const first=h.submit(a.form),b=h.mount(8);
 answer.resolve({bookings:[{id:7}],tariffs:[]});await first;
 assert.equal(h.calls.length,1);assert.equal(b.bills.insertions.length,0);
}
// A registration already sent for A may finish, but its UI result remains on A.
{
 const h=harness(),a=h.mount(7),answer=deferred();
 h.respond(path=>path.startsWith('/workflow')?{bookings:[{id:7}],tariffs:[]}:answer.promise);
 const first=h.submit(a.form);await waitForCalls(h,2);
 assert.equal(h.calls[1].options.body.bookingId,7);const b=h.mount(8);
 answer.resolve({id:1,status:'PENDING',qrPath:'/qr'});await first;
 assert.equal(h.calls.length,2);assert.equal(b.bills.insertions.length,0);assert.equal(b.qr.images.length,0);
}
// Current QR rendering still works, releases its blob, and ignores account-reset replies.
{
 const h=harness(),a=h.mount(7);h.respond(()=>({qr:'A'}));await h.click(a.action('bankQr'));
 assert.equal(a.qr.images.length,1);h.mount(8);assert.deepEqual(h.revoked,['blob:1']);
 const b=h.mount(8),answer=deferred();h.respond(()=>answer.promise);
 const first=h.click(b.action('bankQr'));h.listeners['pb-chat-account-reset']();
 answer.resolve({qr:'B'});await first;assert.equal(b.qr.images.length,0);
}
console.log('PASS: delayed QR, PAID, workflow and registration replies remain bound to their original sale; navigation and account reset cannot show another booking payment');
