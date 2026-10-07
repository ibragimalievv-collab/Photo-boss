import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/localstore.js',import.meta.url),'utf8').replaceAll('export ','');
const saved=new Map(),local=new Map(),session=new Map([['pb_browser_session_only','1']]);
const storage=map=>({getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)});
function context(){const c=vm.createContext({Number,Date,JSON,URLSearchParams,Promise,window:{},localStorage:storage(local),sessionStorage:storage(session),saved});vm.runInContext(source,c);vm.runInContext('localAction=async(store,mode,work)=>work({put:r=>saved.set(r.key,r),get:k=>saved.get(k)});',c);return c;}
let c=context();vm.runInContext('setSnapshotUser(1001)',c);
for(const path of ['/me','/dashboard','/shoot-control','/bookings?day=2026-10-07','/schedule?from=2026-10-07&to=2026-10-13','/delivery/501'])await vm.runInContext(`saveSnapshot(${JSON.stringify(path)},{marker:'owner'})`,c);
c=context();assert.equal((await vm.runInContext("readSnapshot('/me')",c)).marker,'owner');assert.equal((await vm.runInContext("readSnapshot('/delivery/501')",c))._offline,true);
vm.runInContext('setSnapshotUser(null)',c);assert.equal(await vm.runInContext("readSnapshot('/me')",c),null);assert.equal(local.size,0);
vm.runInContext('setSnapshotUser(1002)',c);assert.equal(await vm.runInContext("readSnapshot('/me')",c),null);
for(const path of ['/audit','/finance?period=month','/delivery-contacts','/delivery/501/photos/1']){await vm.runInContext(`saveSnapshot(${JSON.stringify(path)},{marker:'sensitive'})`,c);assert.equal(await vm.runInContext(`readSnapshot(${JSON.stringify(path)})`,c),null);}
console.log('PASS: installed browser cold offline start, scoped shoot/gallery snapshots, account switch, sensitive paths excluded');
