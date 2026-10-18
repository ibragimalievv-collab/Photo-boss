import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/form-state.js',import.meta.url),'utf8').replaceAll('export ','');
const storage=new Map();
const events={};
let forms=[];
const document={querySelectorAll:()=>forms,addEventListener:(name,fn)=>events[name]=fn,createElement:()=>({dataset:{},setAttribute(){}})};
const context=()=>vm.createContext({document,window:{addEventListener(){}},Map,Set,Date,Number,Object,JSON,Boolean,DataTransfer:class{constructor(){this.files=[];this.items={add:f=>this.files.push(f)};}},sessionStorage:{get length(){return storage.size;},key:i=>[...storage.keys()][i],getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}});
let c=context();vm.runInContext(source,c);vm.runInContext('setFormActor(42)',c);
const make=()=>{const fields=[{name:'total',value:'150',type:'number'},{name:'selected',files:[{name:'family.jpg'}],type:'file'}];for(const f of fields)fields[f.name]=f;return {id:'workflowSale',dataset:{draftScope:'7'},elements:fields,querySelector(){return null;},append(n){this.notice=n;}};};
let original=make();forms=[original];vm.runInContext('captureForms()',c);
let restored=make();restored.elements.total.value='';restored.elements.selected.files=[];forms=[restored];vm.runInContext('restoreForms()',c);
assert.equal(restored.elements.total.value,'150');assert.equal(restored.elements.selected.files[0].name,'family.jpg');
vm.runInContext('setFormActor(43)',c);let foreign=make();foreign.elements.total.value='';forms=[foreign];vm.runInContext('restoreForms()',c);assert.equal(foreign.elements.total.value,'','another account must not see the previous draft');
// A real page reload cannot resurrect File handles: restore text and state this explicitly.
c=context();vm.runInContext(source,c);vm.runInContext('setFormActor(42)',c);let reload=make();reload.elements.total.value='';reload.elements.selected.files=[];forms=[reload];vm.runInContext('restoreForms()',c);
assert.equal(reload.elements.total.value,'150');assert.ok(reload.notice.textContent.includes('выберите заново'));
reload.dataset.pendingKeys='["key1"]';vm.runInContext('captureForms()',c);
let queued=make();queued.elements.total.value='';forms=[queued];vm.runInContext('restoreForms()',c);assert.equal(queued.dataset.pendingKeys,'["key1"]');
c.f=queued;vm.runInContext('clearForm(f)',c);assert.equal(storage.size,0);
console.log('PASS: per-account text drafts, redraw file handles, honest reload warning, queued operation keys, clear on completion');

// A server reset clears only this actor's operational drafts; Academy and the
// next actor stay intact, even on first adoption of a nonempty generation.
c=context();vm.runInContext(source,c);vm.runInContext('setFormActor(42)',c);
const sale=make(),training=make(),other=make();training.id='salesTrainingForm';
forms=[sale,training];vm.runInContext('captureForms()',c);
vm.runInContext('setFormActor(43)',c);forms=[other];vm.runInContext('captureForms()',c);
vm.runInContext('setFormActor(42,"reset-1")',c);
assert.ok(!storage.has('pb-form-draft-v1:42:workflowSale:7'));
assert.ok(storage.has('pb-form-draft-v1:42:salesTrainingForm:7'));
assert.ok(storage.has('pb-form-draft-v1:43:workflowSale:7'));
const fresh=make();fresh.elements.total.value='';forms=[fresh];vm.runInContext('restoreForms()',c);assert.equal(fresh.elements.total.value,'');
fresh.elements.total.value='3';vm.runInContext('captureForms()',c);
assert.ok(storage.has('pb-form-draft-v1:42:workflowSale:7:reset-1'));
vm.runInContext('setFormActor(42,"reset-1")',c);fresh.elements.total.value='';vm.runInContext('restoreForms()',c);assert.equal(fresh.elements.total.value,'3');
vm.runInContext('setFormActor(42,"reset-2")',c);fresh.elements.total.value='';vm.runInContext('restoreForms()',c);assert.equal(fresh.elements.total.value,'');
assert.ok(storage.has('pb-form-draft-v1:42:salesTrainingForm:7'));
console.log('PASS: reset generation discards only owned operational drafts, first adoption, repeated login and later reset');
