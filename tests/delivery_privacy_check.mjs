import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the real delivery event handlers without contacting the live app.
const source=fs.readFileSync(new URL('../app/webapp/js/delivery.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const handlers={document:{},window:{}},nodes=[],revoked=[],closeEvents=[];let pending=null,urlId=0;
const element=tag=>({tagName:tag,children:[],handlers:{},open:false,isConnected:false,
 setAttribute(){},append(...children){this.children.push(...children);},
 addEventListener(event,handler){(this.handlers[event]??=[]).push(handler);},
 showModal(){this.open=true;},close(){if(!this.open)return;this.open=false;closeEvents.push(()=>{for(const handler of this.handlers.close||[])handler();});},
 remove(){this.isConnected=false;this.removed=true;},querySelector(){return null;}});
const context=vm.createContext({Blob,Promise,Error,Date,Number,JSON,Set,
 window:{addEventListener(event,handler){(handlers.window[event]??=[]).push(handler);}},
 document:{addEventListener(event,handler){(handlers.document[event]??=[]).push(handler);},createElement:element,
  body:{append(node){node.isConnected=true;nodes.push(node);}}},
 URL:{createObjectURL:()=>`blob:private-${++urlId}`,revokeObjectURL:url=>revoked.push(url)},
 api:async()=>pending||new Blob(['private photo'],{type:'image/jpeg'}),
});
vm.runInContext(source,context);
function emit(surface,event){for(const handler of handlers[surface][event]||[])handler({});}
function flushCloseEvents(){while(closeEvents.length)closeEvents.shift()();}
function setup(){vm.runInContext("shell();bookingId=32;album={photos:[{id:9,name:'private.jpg'}]};dialog.showModal();",context);}
const view=()=>vm.runInContext("click({target:{closest:()=>({dataset:{delivery:'view',photo:'9'}})}})",context);
const viewers=()=>nodes.filter(n=>n.className==='delivery-viewer'&&!n.removed);

setup();
let finish;pending=new Promise(resolve=>{finish=resolve;});
const late=view();vm.runInContext('dialog.close()',context);finish(new Blob(['late photo']));await late;
assert.equal(viewers().length,0,'Closing the album must prevent a delayed photo from opening');
assert.equal(urlId,0,'Discarded responses must not allocate private image URLs');
flushCloseEvents();
pending=null;setup();await view();assert.equal(viewers().length,1);
viewers()[0].children[0].onclick();flushCloseEvents();assert.equal(viewers().length,0);assert.equal(revoked.length,1);

setup();await view();emit('window','hashchange');
assert.equal(viewers().length,0,'Navigation must close the private photo');
assert.equal(vm.runInContext('dialog.open',context),false,'Navigation must close the album');
assert.equal(revoked.length,2);
flushCloseEvents();

setup();await view();emit('document','pb-chat-account-reset');
assert.equal(viewers().length,0,'Account reset must remove the old private photo');
assert.equal(vm.runInContext('album',context),null,'Account reset must clear old gallery metadata');
assert.equal(revoked.length,3);
flushCloseEvents();

for(const [surface,event] of [['document','pb-chat-account-reset'],['window','pagehide']]){
 setup();pending=new Promise(resolve=>{finish=resolve;});const request=view();emit(surface,event);
 finish(new Blob(['stale photo']));await request;pending=null;
 assert.equal(viewers().length,0,`${event} must discard delayed private photos`);
 flushCloseEvents();
}

// Native dialog close events are queued: an old one must not erase a reopened
// album or close a newly opened photo viewer.
setup();vm.runInContext('dialog.close();version++;bookingId=33;album={photos:[]};dialog.showModal()',context);
flushCloseEvents();assert.equal(vm.runInContext('bookingId',context),33);
assert.notEqual(vm.runInContext('album',context),null,'A stale close event must preserve the reopened album');
setup();await view();vm.runInContext('clearPhotoViewer()',context);await view();flushCloseEvents();
assert.equal(viewers().length,1,'The previous photo close event must not remove its replacement');
emit('window','pagehide');flushCloseEvents();
assert.equal(urlId,revoked.length,'Every allocated private image URL must be released');
console.log('PASS: gallery close, navigation, pagehide and account reset discard delayed photos and revoke private image URLs');
