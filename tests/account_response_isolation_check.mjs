import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/api.js',import.meta.url),'utf8')
 .replace("import {readSnapshot,saveSnapshot,setSnapshotUser,reconcileOperationsGeneration} from './localstore.js';",'')
 .replaceAll('export ','');
const fresh=`auth_date=${Math.floor(Date.now()/1000)}&user=${encodeURIComponent('{"id":1001}')}&hash=${'a'.repeat(64)}`;
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

function fixture(){
 const response=deferred(),entered=deferred(),saved=[],identities=[],storage=new Map();
 const context=vm.createContext({URL,URLSearchParams,FormData,AbortController,Date,Number,Promise,setTimeout,clearTimeout,atob,Event,
  location:{hash:'#home',origin:'https://photo-boss.onrender.com'},navigator:{onLine:true},
  window:{Telegram:{WebApp:{initData:fresh}},dispatchEvent(){}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  readSnapshot:async()=>null,saveSnapshot:async(path,data)=>saved.push({path,data}),setSnapshotUser:id=>identities.push(id),reconcileOperationsGeneration:async()=>false,
  fetch:async()=>{entered.resolve();return response.promise;}});
 vm.runInContext(source,context);
 return {context,response,entered,saved,identities,run:code=>vm.runInContext(code,context)};
}
for(const status of [200,401]){
 const f=fixture(),pending=f.run("api('/finance')");
 const rejected=assert.rejects(pending,error=>error.status===409&&/Аккаунт изменился/.test(error.message));
 await f.entered.promise;f.run('preferBrowserSession()');
 f.response.resolve({status,ok:status===200,json:async()=>({privateOwnerData:'must not reach another account'})});
 await rejected;
 assert.equal(f.saved.length,0);
 assert.deepEqual(f.identities,[null]);
}
console.log('PASS: late owner response and expired-session response cannot update another account or its snapshots');

{
 const f=fixture(),body=deferred(),reading=deferred();
 const pending=f.run("api('/finance')");
 const rejected=assert.rejects(pending,error=>error.status===409);
 await f.entered.promise;
 f.response.resolve({status:200,ok:true,json:async()=>{reading.resolve();return body.promise;}});
 await reading.promise;f.run('preferBrowserSession()');body.resolve({privateOwnerData:'late body'});
 await rejected;assert.equal(f.saved.length,0);
}
{
 const f=fixture(),body=deferred(),reading=deferred();
 const pending=f.run("api('/receipts/1',{responseType:'blob'})");
 const rejected=assert.rejects(pending,error=>error.status===409);
 await f.entered.promise;
 f.response.resolve({status:200,ok:true,blob:async()=>{reading.resolve();return body.promise;}});
 await reading.promise;f.run('preferBrowserSession()');body.resolve(new Blob(['private receipt']));
 await rejected;
}
console.log('PASS: account changes during JSON and receipt-image reads discard the private body');

for(const offline of [true,false]){
 const f=fixture(),cached=deferred(),reading=deferred();
 f.context.navigator.onLine=!offline;
 f.context.readSnapshot=async()=>{reading.resolve();return cached.promise;};
 if(!offline)f.context.fetch=async()=>{throw new TypeError('Network disconnected');};
 const pending=f.run("api('/finance')");
 const rejected=assert.rejects(pending,error=>error.status===409);
 await reading.promise;f.run('preferBrowserSession()');cached.resolve({privateOwnerData:'cached'});
 await rejected;assert.equal(f.saved.length,0);
}
{
 const f=fixture(),reconcile=deferred(),reading=deferred();
 f.context.reconcileOperationsGeneration=async()=>{reading.resolve();return reconcile.promise;};
 const pending=f.run("api('/me')");
 const rejected=assert.rejects(pending,error=>error.status===409);
 await f.entered.promise;
 f.response.resolve({status:200,ok:true,json:async()=>({user:{id:1,telegramId:1001},operationsGeneration:'before'})});
 await reading.promise;f.run('preferBrowserSession()');reconcile.resolve(false);
 await rejected;assert.deepEqual(f.identities,[null]);assert.equal(f.saved.length,0);
}
console.log('PASS: late offline snapshots, network-fallback snapshots and account reconciliation cannot restore the previous identity');
