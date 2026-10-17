import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/workflow.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const handlers={},calls=[],nodes=[],revoked=[];let pending=null;
const element=tag=>({tagName:tag,children:[],handlers:{},open:false,
 setAttribute(){},append(...children){this.children.push(...children);},
 addEventListener(event,handler){this.handlers[event]=handler;},
 showModal(){this.open=true;},close(){this.open=false;this.handlers.close?.();},remove(){this.removed=true;}});
const context=vm.createContext({Blob,Promise,Error,Date,Number,JSON,Set,
 window:{addEventListener(event,handler){(handlers[event]??=[]).push(handler);}},
 document:{addEventListener(){},createElement:element,body:{append:node=>nodes.push(node)}},
 URL:{createObjectURL:()=>`blob:private-${nodes.length}`,revokeObjectURL:url=>revoked.push(url)},
 api:async(path,options)=>{calls.push({path,options});if(pending)return pending;return new Blob(['receipt'],{type:'image/jpeg'});},
});
vm.runInContext(source,context);
await vm.runInContext(`workflowClick({dataset:{workflow:'receipt-view',receiptPath:'/receipts/5/image'}})`,context);
assert.equal(calls[0].path,'/receipts/5/image');assert.equal(calls[0].options.responseType,'blob');
assert.equal(nodes[0].open,true);assert.ok(nodes[0].children[1].src.startsWith('blob:private-'));
for(const handler of handlers.hashchange)handler();
assert.equal(nodes[0].removed,true);assert.deepEqual(revoked,['blob:private-0']);
await assert.rejects(vm.runInContext(`workflowClick({dataset:{workflow:'receipt-view',receiptPath:'https://external.example/receipt.jpg'}})`,context),/Некорректная ссылка/);
assert.equal(calls.length,1,'external URLs must never be fetched');

let finish;pending=new Promise(resolve=>{finish=resolve;});
const viewing=vm.runInContext(`workflowClick({dataset:{workflow:'receipt-view',receiptPath:'/sale-drafts/7/receipt/image'}})`,context);
for(const handler of handlers.hashchange)handler();finish(new Blob(['late']));await viewing;
assert.equal(nodes.length,1,'a late response must not reopen a private receipt after navigation');
console.log('PASS: receipt viewer uses authenticated blob API, rejects public URLs and revokes private images on navigation');
