import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const original=fs.readFileSync('app/webapp/js/lunch.js','utf8');
const source=original.replace(/^import .*;\n/gm,'').replaceAll('export ','');
let reads=0;
const c=vm.createContext({Date,Number,Promise,Error,setTimeout,document:{addEventListener(){},querySelector(){return null;}},window:{addEventListener(){}},outboxRows:async()=>[{key:'pending',status:++reads<3?'syncing':'synced'}]});
vm.runInContext(source,c);
const saved=await vm.runInContext("waitForLunch('pending')",c);
assert.equal(saved.status,'synced');assert.equal(reads,3,'Wait for the worker already running in the background');
c.outboxRows=async()=>[{key:'bad',status:'error',error:'Rejected mark'}];
await assert.rejects(vm.runInContext("waitForLunch('bad')",c),/Rejected mark/);
console.log('PASS: lunch waits for background confirmation and reports actual rejection');

// A late response from the previous account/reset must not refill the panel.
const events=new Map(),accountEvents=new Map(),responses=[];
const isolated=vm.createContext({Date,Number,Promise,Error,setTimeout,
 navigator:{onLine:true},
 document:{addEventListener:(name,fn)=>accountEvents.set(name,fn),querySelector:()=>null},
 window:{addEventListener:(name,fn)=>events.set(name,fn)},
 api:()=>new Promise(resolve=>responses.push(resolve)),outboxRows:async()=>[]});
vm.runInContext(source,isolated);
for(const reset of [events.get('pb-operations-reset'),accountEvents.get('pb-chat-account-reset')]){
 const pending=vm.runInContext('loadLunch()',isolated);
 reset();responses.shift()({actorId:100,items:[{id:1}]});await pending;
 assert.equal(vm.runInContext('state',isolated),null);
 assert.equal(vm.runInContext('pendingKey',isolated),null);
}
console.log('PASS: lunch drops old account and reset responses');
const view=vm.createContext({Date,Number,Promise,Error,setTimeout,esc:String,document:{addEventListener(){}},window:{addEventListener(){}}});
vm.runInContext(source,view);
vm.runInContext('state={actorId:1,items:[],timezone:"Europe/Moscow",hotels:[{id:3,name:"First"},{id:9,name:"Planned"}],defaultHotelId:9,canStart:true,canEdit:false}',view);
assert.match(vm.runInContext('lunchView()',view),/<option value="9" selected>Planned/);
vm.runInContext('state.defaultHotelId=null',view);
assert.match(vm.runInContext('lunchView()',view),/<option value="">Выберите отель смены/);
vm.runInContext('selectedHotel=3',view);
assert.match(vm.runInContext('lunchView()',view),/<option value="3" selected>First/);
console.log('PASS: planned hotel selected; ambiguous plans require explicit choice; manual choice retained');
