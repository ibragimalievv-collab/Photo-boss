import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
  .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const rows=new Map(),calls=[];
let active=0,maximum=0,failure=null,seed=0;
const localAction=async(_table,_mode,fn)=>fn({
 get:key=>rows.get(key),
 add:row=>{rows.set(row.key,row);return row;},
 put:row=>{rows.set(row.key,row);return row;}
});
const api=async(_path,{body})=>{
 const operation=body instanceof FormData?JSON.parse(body.get('operation')):body;
 const filename=body instanceof FormData?body.get('file').name:null;
 active++;maximum=Math.max(maximum,active);calls.push(filename);
 await new Promise(resolve=>setTimeout(resolve,8));
 active--;
 if(failure===filename){const err=new Error('Файл отклонён');err.status=413;throw err;}
 return {ok:true,photoId:calls.length};
};
const context=vm.createContext({
 rows,api,localAction,Blob,FormData,Promise,Date,Error,Event,
 crypto:{randomUUID:()=>String(++seed)},navigator:{onLine:true},
 window:{dispatchEvent(){},addEventListener(){}},
 document:{hidden:false,addEventListener(){}},
 setTimeout,clearTimeout,queueMicrotask,
});
vm.runInContext(source,context);
vm.runInContext(`userId=42;outboxRows=async()=>[...rows.values()]
 .map(row=>({...row,hasBlob:Boolean(row.blob),blob:null}))
 .sort((a,b)=>a.createdAt-b.createdAt||(a.order||0)-(b.order||0));`,context);
const photo=name=>Object.assign(new Blob(['photo bytes'],{type:'image/jpeg'}),{name});
const files=[photo('one.jpg'),photo('two.jpg'),photo('three.jpg')];
context.photos=files;
context.events=[];
await vm.runInContext(`enqueueFilesOneByOne(photos,file=>({
 kind:'shoot_photo',date:'2026-10-14',data:{shooting:{id:7}}
}),update=>events.push(update))`,context);
assert.deepEqual(calls,['one.jpg','two.jpg','three.jpg']);
assert.equal(maximum,1,'uploads should never overlap');
assert.equal(context.events.filter(x=>x.state==='synced').length,3);
assert.equal(rows.size,3);
assert.ok([...rows.values()].every(x=>x.status==='synced'&&x.blob===null));

failure='two.jpg';calls.length=0;context.photos=[photo('again-one.jpg'),photo('two.jpg'),photo('not-sent.jpg')];
await assert.rejects(vm.runInContext(`enqueueFilesOneByOne(photos,file=>({
 kind:'shoot_photo',date:'2026-10-14',data:{shooting:{id:7}}
}))`,context),/Остановка на/);
assert.deepEqual(calls,['again-one.jpg','two.jpg'],'third photo must not be transmitted after a failure');
const broken=[...rows.values()].find(row=>row.filename==='two.jpg'&&row.status==='error');
assert.ok(broken?.blob,'failed original must remain in browser queue');
console.log('PASS: selected folder sends each file in order, stops safely, and preserves failed originals');
