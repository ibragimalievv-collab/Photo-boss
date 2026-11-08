import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/workflow.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
const fileSource=fs.readFileSync('app/webapp/js/sale-files.js','utf8');
const {saleSaveState}=await import('data:text/javascript;base64,'+Buffer.from(fileSource).toString('base64'));
// workflow.js imports are stripped. Pure helpers run for real, the price quote
// is the confirmed one a valid sale form produces (pricing math has its own
// check), and anything else this harness does not model gets a neutral stub.
const importedNames=[...fs.readFileSync('app/webapp/js/workflow.js','utf8').matchAll(/^import\s*\{([^}]*)\}\s*from/gm)]
 .flatMap(m=>m[1].split(',').map(x=>x.trim().split(/\s+as\s+/).pop()).filter(Boolean));
const realistic={...await import('../app/webapp/js/guest-phone.js'),...await import('../app/webapp/js/receipt-files.js'),
 galleryRecoveryKeys:(_queue,keys)=>keys, // no gallery photos in these scenarios
 updatePricePreview:(form,b)=>{const amount=Number(b?.price||0)*Number(form?.values?.sold||0);return {amount,due:0,creditAmount:0,photoAmount:null,tariffId:null,discountId:null,albumId:null,albumQuantity:null};}};
const stubImports=context=>{for(const name of importedNames)if(!(name in context))context[name]=realistic[name]||(()=>'');};
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const response=(selected=7)=>({actorId:42,bookings:[7,8].map(id=>({id,status:'READY_FOR_SALE',price:'400',paidAmount:'400',sales:[],totalFrames:10,marker:selected})),tariffs:[]});
function harness(){
 const windowListeners={},documentListeners={},enqueued=[],requests=[],app={innerHTML:'workflow'},location={hash:'#workflow'};
 let respond=async()=>response(),rows=async()=>[],validate=async()=>{},mounted=null,replaced=null;
 const context=vm.createContext({rejectedInlineSale:()=>null,saleSaveState,Number,JSON,Error,Set,Map,Date,Intl,setTimeout,clearTimeout,location,
  navigator:{onLine:true},sessionStorage:{getItem(){return null;},setItem(){}},URL:{revokeObjectURL(){}},
  FormData:class{constructor(form){this.values=form.values;}get(key){return this.values[key];}},
  crypto:{randomUUID:()=>`key-${enqueued.length}-${requests.length}`},
  captureForm(){},captureForms(){},restoreForms(){},clearForm(){},todayKey:()=> '2026-10-26',
  checkSaleFiles:files=>validate(files),outboxRows:()=>rows(),enqueueBatch:async items=>enqueued.push(...items),
  canReplaceDiskSelection:row=>Boolean(row?.replaceable),selectedDiskPhotos:()=>[{path:'disk:/new/photo.jpg',revision:'new'}],
  replaceDiskSelection:async(...args)=>{replaced=args;},
  window:{addEventListener(name,fn){(windowListeners[name]??=[]).push(fn);}},
  document:{addEventListener(name,fn){(documentListeners[name]??=[]).push(fn);},
   querySelector:selector=>selector==='#app'?app:selector==='#workflowSale'?mounted:null,querySelectorAll:()=>[]},
  api:async(path,options)=>{requests.push({path,options});return respond(path,options);},
 });
 stubImports(context);
 vm.runInContext(source,context);
 vm.runInContext('savedUpdateFormProgress=updateFormProgress;workflowView=()=>"workflow";mountDiscountPanel=()=>{};updateFormProgress=()=>{};refreshQuote=()=>{};',context);
 context.initial=response();vm.runInContext('data=initial;selected=7;',context);
 function form(id=7){
  const files=[{type:'image/jpeg',name:'original.jpg',size:1234}],receipt=[{type:'image/jpeg',name:'receipt.jpg',size:123}];
  mounted={setAttribute(){},removeAttribute(){},id:'workflowSale',isConnected:true,dataset:{draftScope:String(id)},classList:{contains:()=>false},
   values:{total:'10',sold:'1'},elements:{selected:{files},receipt:{files:receipt}}};
  return mounted;
 }
 return {context,location,app,form,requests,enqueued,
  respond:fn=>{respond=fn;},rows:fn=>{rows=fn;},validate:fn=>{validate=fn;},
  run:script=>vm.runInContext(script,context),select:id=>{context.nextId=id;vm.runInContext('workflowSelect(nextId)',context);},
  emitWindow:name=>{for(const fn of windowListeners[name]||[])fn();},
  reset:()=>{for(const fn of documentListeners['pb-chat-account-reset']||[])fn();},
  submit:form=>{context.formFixture=form;return vm.runInContext('workflowSubmit(formFixture)',context);},
  click:button=>{context.buttonFixture=button;return vm.runInContext('workflowClick(buttonFixture)',context);},
  replacement:()=>replaced,
 };
}

// An older selection must never win merely because its HTTP response arrived last.
{
 const h=harness(),a=deferred(),b=deferred();h.respond(path=>path.endsWith('=7')?a.promise:b.promise);
 const first=h.run('loadWorkflow(7)'),second=h.run('loadWorkflow(8)');
 b.resolve(response(8));assert.equal(await second,true);a.resolve(response(7));assert.equal(await first,false);
 assert.equal(h.run('selected'),8);assert.equal(h.run('data.bookings[0].marker'),8);
}
// Changing the actual picker while a refresh/outbox read is pending invalidates it.
{
 const h=harness(),pending=deferred(),entered=deferred();h.rows(()=>{entered.resolve();return pending.promise;});
 const first=h.run('loadWorkflow(7)');await entered.promise;h.select(8);pending.resolve([]);
 assert.equal(await first,false);assert.equal(h.run('selected'),8);
}
// A stale form cannot create an operation for the new selected booking.
{
 const h=harness(),form=h.form(7),files=form.elements.selected.files;h.select(8);
 await assert.rejects(h.submit(form),/Съёмка или аккаунт изменились/);
 assert.equal(h.enqueued.length,0);assert.equal(form.elements.selected.files,files);
 const live=h.form(8);await h.submit(live);
 assert.equal(h.enqueued.find(row=>row.kind==='sale_start').data.booking.id,8);
}
// Selection/account changes during file validation preserve files and enqueue nothing.
for(const action of ['select','reset']){
 const h=harness(),form=h.form(7),files=form.elements.selected.files,gate=deferred(),entered=deferred();
 h.validate(()=>{entered.resolve();return gate.promise;});const first=h.submit(form);await entered.promise;
 if(action==='select')h.select(8);else h.reset();gate.resolve();
 await assert.rejects(first,/Съёмка или аккаунт изменились/);assert.equal(h.enqueued.length,0);
 assert.equal(form.elements.selected.files,files);assert.equal(form.dataset.pendingKeys,undefined);
}
// Both browser hash navigation and history.replaceState-style navigation keep home mounted.
for(const signal of ['hashchange','pagehide','replaceState']){
 const h=harness(),gate=deferred();h.respond(()=>gate.promise);
 const first=h.click({dataset:{workflow:'refresh'}});h.app.innerHTML='home';
 if(signal==='replaceState')h.location.hash='#home';else h.emitWindow(signal);
 gate.resolve(response());await first;assert.equal(h.app.innerHTML,'home');
}
// A completed server takeover is retained but cannot redraw another screen after its POST.
{
 const h=harness(),gate=deferred();h.respond(()=>gate.promise);
 const first=h.click({dataset:{workflow:'takeover-sale',booking:'7'}});
 h.location.hash='#home';h.app.innerHTML='home';gate.resolve({ok:true});await first;
 assert.equal(h.requests.length,1);assert.equal(h.app.innerHTML,'home');
}
// Disk recovery can only replace a key owned by the currently open draft.
{
 const h=harness(),form=h.form(7);form.dataset.pendingKeys='["disk-A"]';
 h.run('data.bookings[0].draft={id:91,mine:true};');h.rows(async()=>[{key:'disk-A',kind:'sale_disk_selected',replaceable:true}]);
 await assert.rejects(h.click({dataset:{workflow:'replace-disk-photo',key:'disk-B'}}),/исходную продажу/);
 assert.equal(h.replacement(),null);await h.click({dataset:{workflow:'replace-disk-photo',key:'disk-A'}});
 assert.equal(h.replacement()[0],'disk-A');assert.equal(h.replacement()[2],91);
}
// A definitive Disk rejection unlocks source selection, never accepted financial fields.
{
 const h=harness(),form=h.form(7),parts=new Map(),diskControls=[{disabled:true},{disabled:true}],submit={};
 form.dataset.pendingKeys='["start","disk-A","finish"]';
 const financial=['total','sold','selected'].map(name=>({name,type:'text',disabled:false}));
 for(const field of financial)financial[field.name]=field;form.elements=financial;
 form.querySelectorAll=()=>diskControls;
 form.querySelector=selector=>selector==='button[type="submit"],button:not([type])'?submit:parts.get(selector)||null;
 form.append=node=>{if('operationProgress' in node.dataset)parts.set('[data-operation-progress]',node);if('salePhotoProgress' in node.dataset)parts.set('[data-sale-photo-progress]',node);};
 h.context.document.querySelectorAll=()=>[form];h.context.document.createElement=()=>({dataset:{},setAttribute(){},append(){},remove(){}});
 h.context.saleQueueRepairable=()=>false;h.context.rejectedBooking=()=>null;
 h.context.progressRows=[{key:'start',kind:'sale_start',status:'synced'},
  {key:'disk-A',kind:'sale_disk_selected',status:'error',replaceable:true,error:'Папка изменилась'},
  {key:'finish',kind:'sale_complete',status:'local'}];
 h.run('queue=progressRows;savedUpdateFormProgress();');
 assert.ok(financial.every(field=>field.disabled));assert.ok(diskControls.every(control=>!control.disabled));
 assert.match(parts.get('[data-operation-progress]').textContent,/выберите один новый кадр/);
}
console.log('PASS: out-of-order sale loads, picker/account/navigation changes and stale forms cannot submit another booking; files and explicit same-draft Disk recovery survive');
