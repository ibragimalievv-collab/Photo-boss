import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/bank-payments.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const deferred=()=>{let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};};
const pending=(id=1,bookingId=7,extra={})=>({id,bookingId,amount:'1000.00',status:'PENDING',url:'https://qr.nspk.ru/test',qrPath:`/bank-payments/${id}/qr`,testMode:false,provider:'sber_qr',...extra});
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
function harness({hidden=false,online=true}={}){
 const listeners={},windowListeners={},calls=[],events=[],created=[],revoked=[],timers=new Map();
 let panel=null,booking=null,settings=null,respond=async(path)=>path.startsWith('/workflow')?{...settings,bookings:[booking],tariffs:[]}:pending(),clock=0,serial=0,quotes=0;
 const files=Array.from({length:20},(_,id)=>({name:`original-${id}.jpg`}));
 const sale={dataset:{},elements:{total:{value:'150'},sold:{value:'20'},selected:{files}}};
 const document={hidden,addEventListener:(name,fn)=>{listeners[name]=fn;},querySelector:selector=>selector==='#bankPanel'?panel:selector==='#workflowSale'?sale:null,createElement:()=>({style:{}})};
 const navigator={onLine:online};
 const context=vm.createContext({Number,JSON,Error,Set,Date:{now:()=>clock},navigator,document,
  setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,due:clock+delay,delay});return id;},clearTimeout:id=>timers.delete(id),
  esc:value=>String(value??''),crypto:{randomUUID:()=> 'invoice-request'},
  FormData:class{constructor(form){this.form=form;}get(key){return this.form.values[key];}},
  Event:class{constructor(type){this.type=type;}},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},
  URL:{createObjectURL(blob){created.push(blob);return 'blob:'+created.length;},revokeObjectURL:url=>revoked.push(url)},
  updatePricePreview:()=>{quotes++;return {tariffId:1};},outboxRows:async()=>[],
  api:async(path,options)=>{calls.push({path,options});return respond(path,options);},
  window:{addEventListener:(name,fn)=>{windowListeners[name]=fn;},dispatchEvent:event=>events.push(event)},
 });
 vm.runInContext(source,context);
 function mount(id=7,bills=[pending(1,id)],overrides={}){
  const qr={innerHTML:'',textContent:'',images:[],append(image){this.images.push(image);}};
  const rows=new Map(bills.map(bill=>[bill.id,{outerHTML:'',message:{textContent:''}}]));
  const message={textContent:''},submit={disabled:false};
  const mounted={qr,rows,message,submit};
  const billsBox={insertions:[],insertAdjacentHTML(_position,html){this.insertions.push(html);const id=Number(html.match(/data-bank-bill="(\d+)"/)[1]);rows.set(id,{outerHTML:html,message:{textContent:''}});}};
  const form={id:'bankPaymentForm',dataset:{},values:{purpose:'PURCHASE',amount:''},closest:()=>mounted,querySelector:selector=>selector==='button'?submit:message};
  mounted.form=form;mounted.bills=billsBox;
  mounted.querySelector=selector=>{
   if(selector==='#bankQR')return qr;if(selector==='#bankBills')return billsBox;if(selector==='#bankPaymentForm [role=status]')return message;
   const id=Number(selector.match(/data-bank-bill="(\d+)"/)?.[1]);return selector.includes('[role=status]')?rows.get(id)?.message:rows.get(id)||null;
  };
  mounted.action=(kind,billId=1)=>{const button={dataset:{[kind]:String(billId)},disabled:false};button.closest=selector=>selector==='#bankPanel'?mounted:selector==='[data-bank-bill]'?rows.get(billId):button;return button;};
  panel=mounted;booking={id,sales:[],bankPayments:bills,paidAmount:'0'};settings={actorId:42,bankConfigured:true,bankProvider:'sber_qr',bankTestMode:false,tariffs:[],...overrides};
  context.fixture=booking;context.fixtureSettings=settings;
  mounted.html=vm.runInContext('bankPanel(fixture,fixtureSettings)',context);return mounted;
 }
 function fire(){assert.ok(timers.size,'Expected a scheduled bank status check');const [id,task]=[...timers].sort((a,b)=>a[1].due-b[1].due)[0];timers.delete(id);clock=Math.max(clock,task.due);return Promise.resolve(task.fn());}
 return {mount,calls,events,created,revoked,timers,files,sale,document,navigator,context,
  respond:fn=>{respond=fn;},get quotes(){return quotes;},get panel(){return panel;},get booking(){return booking;},get settings(){return settings;},
  fire,delay:()=>Math.min(...[...timers.values()].map(t=>t.delay)),advance:ms=>{clock+=ms;},
  click:button=>listeners.click({target:button}),window:name=>windowListeners[name](),event:name=>listeners[name](),
  hide(value){document.hidden=value;listeners.visibilitychange();},online(value){navigator.onLine=value;windowListeners[value?'online':'offline']();},
 };
}

// Real bank confirmation updates one invoice/quote; selected originals never get rebuilt.
{
 const h=harness(),p=h.mount();let status=0;
 h.respond(path=>path.startsWith('/workflow')?{...h.settings,bookings:[{...h.booking,paidAmount:'1000'}],tariffs:[]}:pending(1,7,{status:++status===1?'PENDING':'PAID'}));
 assert.equal(h.timers.size,1);await h.fire();assert.equal(h.delay(),10000);assert.equal(h.events.length,0);
 await h.fire();assert.equal(h.timers.size,0);assert.equal(h.quotes,1);assert.equal(h.events.length,1);
 assert.equal(h.events[0].type,'pb-bank-paid');assert.equal(h.events[0].detail.bookingId,7);assert.equal(h.events[0].detail.billId,1);
 assert.match(p.qr.textContent,/Банк подтвердил/);assert.match(p.rows.get(1).outerHTML,/Поступление подтверждено/);
 assert.equal(h.sale.elements.selected.files,h.files);assert.equal(h.files.length,20);assert.equal(h.sale.elements.sold.value,'20');
 assert.equal(h.calls.filter(c=>c.path==='/bank-payments').length,0,'Polling cannot register a new invoice');
}
// A manual check shares the in-flight automatic request; repeated mount events add no timers.
{
 const h=harness(),p=h.mount(),answer=deferred();h.respond(()=>answer.promise);
 const automatic=h.fire();await flush();h.window('pb-workflow-mounted');h.window('pb-workflow-mounted');
 const manual=h.click(p.action('bankCheck'));await flush();assert.equal(h.calls.length,1);assert.equal(h.timers.size,0);
 answer.resolve(pending());await Promise.all([automatic,manual]);assert.equal(h.timers.size,1);
}
// Test payments, including inconsistent PAID+testMode, never announce real revenue.
for(const bill of [pending(1,7,{status:'TEST_PAID',testMode:true}),pending(1,7,{status:'PAID',testMode:true}),{...pending(1,7,{status:'PAID'}),testMode:undefined}]){
 const h=harness();const p=h.mount(7,[pending(1,7,{testMode:true})],{bankTestMode:true});h.respond(()=>bill);
 await h.fire();assert.equal(h.calls.length,1);assert.equal(h.quotes,0);assert.equal(h.events.length,0);assert.equal(h.timers.size,0);assert.match(p.qr.textContent,/Тестовая оплата/);
}
// A real pre-existing ecom bill is not reclassified by the newly selected sandbox provider.
{
 const h=harness();h.mount(7,[pending()],{bankTestMode:true});h.respond(path=>path.startsWith('/workflow')?{...h.settings,bookings:[h.booking]}:pending(1,7,{status:'PAID',testMode:false,provider:'sber_ecom'}));
 await h.fire();assert.equal(h.events.length,1);
}
// Hidden and offline panels do not start requests; visibility/connectivity resume one timer.
{
 const h=harness({hidden:true,online:false});h.mount();assert.equal(h.timers.size,0);
 h.online(true);assert.equal(h.timers.size,0);h.hide(false);assert.equal(h.timers.size,1);h.window('pb-workflow-mounted');assert.equal(h.timers.size,1);
 await h.fire();assert.equal(h.calls.length,1);h.online(false);assert.equal(h.timers.size,0);h.online(true);assert.equal(h.timers.size,1);h.hide(true);assert.equal(h.timers.size,0);
}
// A response received while hidden is ignored and queried again after returning.
{
 const h=harness(),p=h.mount(),answer=deferred();h.respond(()=>answer.promise);
 const task=h.fire();await flush();h.hide(true);answer.resolve(pending(1,7,{status:'PAID'}));await task;
 assert.equal(h.events.length,0);assert.equal(p.rows.get(1).outerHTML,'');assert.equal(h.timers.size,0);
 h.respond(()=>pending());h.hide(false);await h.fire();assert.equal(h.calls.length,2);assert.equal(h.timers.size,1);
}
// Going offline during the workflow refresh cannot announce a paid sale in the background.
{
 const h=harness();h.mount();const answer=deferred();h.respond(path=>path.startsWith('/workflow')?answer.promise:pending(1,7,{status:'PAID'}));
 const task=h.fire();await flush();assert.equal(h.calls.length,2);h.online(false);answer.resolve({...h.settings,bookings:[h.booking]});await task;
 assert.equal(h.events.length,0);assert.equal(h.timers.size,0);h.online(true);assert.equal(h.timers.size,1);
 h.respond(path=>path.startsWith('/workflow')?{...h.settings,bookings:[h.booking]}:pending(1,7,{status:'PAID'}));await h.fire();assert.equal(h.events.length,1);
}
// Navigating, changing accounts or replacing the selected booking invalidate late answers.
for(const event of ['hashchange','pagehide','pb-operations-reset','pb-chat-account-reset']){
 const h=harness(),p=h.mount(),answer=deferred();h.respond(()=>answer.promise);const task=h.fire();await flush();
 if(event==='pb-chat-account-reset')h.event(event);else h.window(event);
 answer.resolve(pending(1,7,{status:'PAID'}));await task;h.online(false);h.online(true);
 assert.equal(p.rows.get(1).outerHTML,'');assert.equal(h.events.length,0);assert.equal(h.timers.size,0);
}
{
 const h=harness();h.mount();const answer=deferred();h.respond(()=>answer.promise);const old=h.fire();await flush();const next=h.mount(8,[pending(2,8)]);
 answer.resolve(pending(1,7,{status:'PAID'}));await old;assert.equal(next.rows.get(2).outerHTML,'');assert.equal(h.events.length,0);assert.equal(h.timers.size,1);
}
// Retry with bounded backoff; authorization and ownership errors stop automatic attempts.
{
 const h=harness(),p=h.mount();h.respond(()=>{const error=new Error('Банк временно недоступен');error.status=503;throw error;});
 for(const delay of [20000,40000,60000,60000]){await h.fire();assert.equal(h.delay(),delay);assert.equal(h.timers.size,1);}
 assert.match(p.rows.get(1).message.textContent,/временно недоступен/);
}
for(const status of [401,403,404,409]){
 const h=harness();h.mount();h.respond(()=>{const error=new Error('Проверьте доступ');error.status=status;throw error;});await h.fire();assert.equal(h.timers.size,0);
}
// Mismatched invoice or booking responses cannot be applied to the current sale.
for(const response of [pending(2),pending(1,8)]){
 const h=harness(),p=h.mount();h.respond(()=>response);await h.fire();assert.equal(p.rows.get(1).outerHTML,'');assert.equal(h.events.length,0);assert.match(p.rows.get(1).message.textContent,/не совпал/);
}
// An abandoned queued timer and a long-running pending order both stop within their budget.
{
 const h=harness();h.mount();h.advance(20*60*1000);await h.fire();assert.equal(h.calls.length,0);assert.equal(h.timers.size,0);
}
{
 const h=harness(),p=h.mount();for(let count=0;count<120;count++)await h.fire();assert.equal(h.calls.length,120);assert.equal(h.timers.size,0);assert.match(p.message.textContent,/Автоматическая проверка завершена/);
}
// A late QR image cannot replace the confirmed-payment message.
{
 const h=harness(),p=h.mount(),image=deferred();h.respond(path=>path.endsWith('/qr')?image.promise:path.startsWith('/workflow')?{...h.settings,bookings:[h.booking]}:pending(1,7,{status:'PAID'}));
 const show=h.click(p.action('bankQr'));await flush();await h.fire();image.resolve({oldQr:true});await show;assert.equal(h.created.length,0);assert.match(p.qr.textContent,/Банк подтвердил/);
}
// A terminal rejection releases the old QR, and the bill provider owns its label.
{
 const h=harness(),p=h.mount(7,[pending(1,7,{provider:'sber_ecom'})]);
 h.respond(path=>path.endsWith('/qr')?{image:true}:pending(1,7,{status:'DECLINED'}));
 await h.click(p.action('bankQr'));assert.equal(p.qr.images[0].alt,'QR платёжной страницы СберБизнес');
 await h.fire();assert.deepEqual(h.revoked,['blob:1']);assert.equal(p.qr.textContent,'Отклонён банком');assert.equal(h.timers.size,0);
}
// Disabled QR.API and test mode use their own wording, without presenting ecom configuration.
{
 const h=harness();let p=h.mount(7,[],{bankConfigured:false,bankProvider:'sber_qr'});assert.match(p.html,/QR.API ещё не готов/);assert.doesNotMatch(p.html,/должен подключить интернет-эквайринг/);assert.equal(h.timers.size,0);
 p=h.mount(7,[],{bankTestMode:true});assert.match(p.html,/Тестовая среда Сбера/);assert.match(p.html,/QR формируется банком/);
 p=h.mount(7,[],{bankProvider:'sber_ecom'});assert.match(p.html,/платёжную страницу банка/);
}
console.log('PASS: bounded sequential bank polling, manual request deduplication, real/test payment separation, hidden/offline/navigation guards, backoff, identity checks and selected-original preservation');
