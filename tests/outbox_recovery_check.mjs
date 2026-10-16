import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/outbox.js',import.meta.url),'utf8')
 .replace(/^import .*;\n/gm,'').replaceAll('export ','');
const rows=new Map(),calls=[],timers=[];
let now=100000,mode='rate';
const photo=new Blob(['original bytes'],{type:'image/jpeg'});
const row=(key,extra={})=>({key,userId:42,kind:'shoot_photo',date:'2026-10-16',data:{shooting:{id:7}},
 filename:key+'.jpg',blob:photo,status:'local',createdAt:1,order:rows.size,...extra});
rows.set('original-key',row('original-key'));
const context=vm.createContext({rows,Blob,FormData,Promise,Event,Error,Date:{now:()=>now},
 navigator:{onLine:true},window:{dispatchEvent(){},addEventListener(){}},
 setTimeout(_fn,delay){timers.push(delay);return timers.length;},clearTimeout(){},
 localAction:async(_store,_mode,fn)=>fn({get:key=>rows.get(key),put:r=>rows.set(r.key,r)}),
 api:async(_path,{body})=>{
  const op=JSON.parse(body.get('operation'));calls.push(op.key);
  assert.equal(await body.get('file').text(),'original bytes');
  if(mode==='rate'){const e=new Error('Slow down');e.status=429;e.retryAfterMs=61000;throw e;}
  if(mode==='invalid'){const e=new Error('Не удалось прочитать вложение. Повторите отправку.');e.status=400;throw e;}
  return {ok:true,photoId:9};
 }});
vm.runInContext(source,context);
vm.runInContext(`userId=42;outboxRows=async()=>[...rows.values()].map(r=>({...r,hasBlob:!!r.blob,blob:null}));`,context);
let result=await vm.runInContext('flushOutbox()',context);
assert.equal(result.temporary,1);assert.equal(rows.get('original-key').status,'local');
assert.ok(rows.get('original-key').blob);assert.equal(rows.get('original-key').retryAt,161000);
assert.ok(timers.at(-1)>=61000);
await vm.runInContext('retryOperation("original-key")',context);
assert.equal(calls.length,1,'manual retry must respect Retry-After');
now=162000;mode='ok';
await vm.runInContext('flushOutbox()',context);
assert.equal(rows.get('original-key').status,'synced');assert.equal(rows.get('original-key').blob,null);
assert.deepEqual(calls,['original-key','original-key'],'replay must preserve operation identity');
rows.clear();calls.length=0;mode='invalid';
rows.set('legacy',row('legacy',{status:'error',errorStatus:400,error:'Не удалось прочитать вложение. Повторите отправку; если ошибка повторится, выберите файл заново.'}));
rows.set('unrelated',row('unrelated',{status:'error',errorStatus:400,error:'Неверное поле'}));
rows.set('forbidden',row('forbidden',{status:'error',errorStatus:403,error:'Нет доступа'}));
await vm.runInContext('flushOutbox()',context);
await vm.runInContext('flushOutbox()',context);
assert.deepEqual(calls,['legacy'],'only legacy media failure may be rechecked, once');
assert.ok(rows.get('legacy').legacyMediaRecovered);assert.ok(rows.get('legacy').blob);
console.log('PASS: 429 honors Retry-After, preserves original/key, and legacy false-400 errors recover once without retrying access denials');
