// Full-shoot folder upload: parallel lanes, resume by checksum, reuse of
// guest-selected originals, and one bad file never stopping the rest.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
  .replace(/^import .*;\n/gm,'').replaceAll('export ','');

function harness({stored=[],selected=[],reject={},linkMissing=new Set(),open=true}={}){
 const rows=new Map(),calls=[];let active=0,maximum=0,seed=0;
 const req=r=>{const o={result:r};queueMicrotask(()=>o.onsuccess?.());return o;};
 // Minimal IndexedDB double for update(): one shared map, generation marker ''.
 const openLocal=async()=>({close(){},transaction(){
  const tx={objectStore:name=>name==='snapshots'?{get:()=>req({generation:''})}:{get:key=>req(rows.get(key)),put:row=>{rows.set(row.key,structuredClone({...row,blob:null}));rows.get(row.key).blob=row.blob;}}};
  queueMicrotask(()=>setTimeout(()=>tx.oncomplete?.(),0));return tx;}});
 const localAction=async(_t,_m,fn)=>fn({get:key=>rows.get(key),put:row=>rows.set(row.key,row),add:row=>rows.set(row.key,row),delete:key=>rows.delete(key)});
 const writeOperations=async items=>{for(const row of items)rows.set(row.key,row);return true;};
 const digestOf=async bytes=>Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex');
 const api=async(path,{body}={})=>{
  if(path.includes('/photo-hashes'))return {stored,selected,open};
  const operation=body instanceof FormData?JSON.parse(body.get('operation')):body;
  const name=operation.data.filename;
  active++;maximum=Math.max(maximum,active);calls.push({kind:operation.kind,name,bytes:body instanceof FormData});
  await new Promise(r=>setTimeout(r,15));active--;
  if(operation.kind==='shoot_photo_link'&&linkMissing.has(name)){const e=new Error('Кадр не найден среди выбранных. Загрузите файл обычным способом.');e.status=409;throw e;}
  if(reject[name]){const e=new Error(reject[name].message);e.status=reject[name].status;throw e;}
  return {ok:true,photoId:calls.length,shootingId:7};
 };
 const context=vm.createContext({rows,api,localAction,openLocal,writeOperations,receiptPayload:async b=>b,Blob,FormData,Promise,Date,Error,Event,Array,Uint8Array,Set,Map,Math,Number,String,JSON,
  crypto:{randomUUID:()=>'k'+String(++seed).padStart(20,'0'),subtle:webcrypto.subtle},navigator:{onLine:true},
  window:{dispatchEvent(){},addEventListener(){}},document:{hidden:false,addEventListener(){}},structuredClone,
  setTimeout,clearTimeout,queueMicrotask,console});
 vm.runInContext(source,context);
 vm.runInContext(`userId=42;operationsGeneration='';outboxRows=async()=>[...rows.values()].map(row=>({...row,hasBlob:Boolean(row.blob),blob:null})).sort((a,b)=>a.createdAt-b.createdAt||(a.order||0)-(b.order||0));`,context);
 return {context,rows,calls,digestOf,stats:()=>({maximum})};
}
const photo=(name,text=name)=>Object.assign(new Blob([text],{type:'image/jpeg'}),{name});
const run=(h,files,opts='{}')=>{h.context.files=files;h.context.events=[];return vm.runInContext(`enqueueShootFiles(files,7,e=>events.push(e),${opts})`,h.context);};

// 0. Adaptive parallelism: grow while speed improves, back off on trouble.
{
 const h=harness();const next=(c,o)=>vm.runInContext(`nextConcurrency(${c},${JSON.stringify(o)})`,h.context);
 assert.equal(next(3,{previousRate:0,rate:100,transient:false}),4,'first measurement probes one more lane');
 assert.equal(next(4,{previousRate:100,rate:130,transient:false}),5,'faster: add a lane');
 assert.equal(next(5,{previousRate:130,rate:132,transient:false}),5,'flat: keep');
 assert.equal(next(5,{previousRate:130,rate:90,transient:false}),4,'slower: drop a lane');
 assert.equal(next(3,{previousRate:100,rate:200,transient:true}),2,'server trouble: drop a lane');
 assert.equal(next(6,{previousRate:100,rate:200,transient:false}),6,'never above six');
 assert.equal(next(2,{previousRate:100,rate:10,transient:true}),2,'never below two');
}
// 1. Parallel and adaptive (2…6 lanes), every file sent once, names kept,
//    and in-flight files always occupy distinct lanes.
{
 const h=harness();const files=Array.from({length:30},(_,i)=>photo(`DSC_${i}.jpg`));
 const result=await run(h,files);
 assert.equal(result.uploaded,30);assert.equal(result.failed.length,0);
 assert.ok(h.stats().maximum>3,'parallelism grows when the link is not saturated');
 assert.ok(h.stats().maximum<=6,'never more than six at once');
 assert.ok([...h.rows.values()].every(r=>Number.isInteger(r.lane)&&r.lane>=0&&r.lane<6));
 assert.deepEqual(h.calls.map(c=>c.name).sort(),files.map(f=>f.name).sort());
 assert.ok([...h.rows.values()].every(r=>r.status==='synced'&&r.data.filename.startsWith('DSC_')));
}
// 2. Resume: files already stored are skipped; selected originals are linked without bytes.
{
 const files=[photo('a.jpg'),photo('b.jpg'),photo('c.jpg'),photo('c-copy.jpg','c.jpg')];
 const probe=harness();const [da,db]=await Promise.all([probe.digestOf(await files[0].arrayBuffer()),probe.digestOf(await files[1].arrayBuffer())]);
 const h=harness({stored:[da],selected:[db]});
 const result=await run(h,files);
 assert.equal(result.skipped,2,'stored file and identical duplicate in the folder are skipped');
 assert.equal(result.linked,1);assert.equal(result.uploaded,1);
 const link=h.calls.find(c=>c.kind==='shoot_photo_link');
 assert.equal(link.name,'b.jpg');assert.equal(link.bytes,false,'selected original must not be re-sent');
}
// 2b. Phone-side reduction keeps the original checksum for resume; frames
//     already stored as reduced copies are recognised by that checksum.
{
 const files=[photo('big1.jpg'),photo('big2.jpg')];
 const probe=harness();const d1=await probe.digestOf(await files[0].arrayBuffer());
 const h=harness({stored:[d1]});
 const result=await run(h,files,'{compress:true}');
 assert.equal(result.skipped,1);assert.equal(result.uploaded,1);
 const row=[...h.rows.values()].find(r=>r.kind==='shoot_photo');
 assert.equal(row.data.source_sha256,await probe.digestOf(await files[1].arrayBuffer()));
 assert.equal(row.data.filename,'big2.jpg');
}
// 3. A file-specific rejection does not stop the rest; a missing selected link falls back to upload.
{
 const h=harness({selected:[],reject:{'bad.jpg':{status:413,message:'Фото должно быть не больше 20 МБ.'}}});
 const files=[photo('one.jpg'),photo('bad.jpg'),photo('two.jpg'),photo('three.jpg')];
 const result=await run(h,files);
 assert.equal(result.uploaded,3);assert.equal(JSON.stringify(result.failed.map(f=>f.name)),'["bad.jpg"]');
 assert.ok(h.calls.some(c=>c.name==='three.jpg'),'files after a bad one are still sent');
 const sel=new Blob(['sel'],{type:'image/jpeg'});sel.name='sel.jpg';
 const p=harness();const d=await p.digestOf(await sel.arrayBuffer());
 const f=harness({selected:[d],linkMissing:new Set(['sel.jpg'])});
 const fallback=await run(f,[sel]);
 assert.equal(fallback.uploaded,1);assert.equal(fallback.linked,0);
 assert.deepEqual(f.calls.map(c=>c.kind),['shoot_photo_link','shoot_photo']);
}
// 4. A shoot-wide rejection (closed upload) pauses the batch with a clear message.
{
 const h=harness({reject:{'x1.jpg':{status:409,message:'Загрузка новых кадров уже закрыта или ещё недоступна.'}}});
 const files=Array.from({length:8},(_,i)=>photo(`x${i}.jpg`));
 await assert.rejects(run(h,files,'{concurrency:2}'),/закрыта.*Принято/);
 assert.ok(h.calls.length<8,'remaining files are not sent after a shoot-wide rejection');
 const closed=harness({open:false});
 await assert.rejects(run(closed,[photo('late.jpg')]),/уже закрыта/);
 assert.equal(closed.calls.length,0);
}
// 5. shoot_complete waits for every photo of the same shoot in the parallel lanes.
{
 const h=harness();
 vm.runInContext(`window.__order=[];`,h.context);
 const origApi=h.context.api;
 h.context.files=[photo('p1.jpg'),photo('p2.jpg'),photo('p3.jpg')];
 await vm.runInContext(`enqueueBatch([...files.map(f=>({kind:'shoot_photo',date:'2026-11-08',data:{shooting:{id:7},filename:f.name},blob:f})),{kind:'shoot_complete',date:'2026-11-08',data:{shooting:{id:7}}}])`,h.context);
 for(let i=0;i<200&&![...h.rows.values()].every(r=>r.status==='synced');i++)await new Promise(r=>setTimeout(r,25));
 const order=h.calls.map(c=>c.kind);
 assert.equal(order.at(-1),'shoot_complete','shoot must close only after all its photos');
 assert.equal(order.filter(k=>k==='shoot_photo').length,3);
}
console.log('PASS: full-shoot upload is parallel, resumable, reuses selected originals and isolates bad files');
