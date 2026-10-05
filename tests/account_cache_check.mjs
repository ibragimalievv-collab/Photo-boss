import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/localstore.js',import.meta.url),'utf8').replaceAll('export ','');
const saved=new Map(),session=new Map();
const context=vm.createContext({Number,Date,JSON,URLSearchParams,Promise,
 window:{Telegram:{WebApp:{initData:'user='+encodeURIComponent('{"id":1001}')}}},
 sessionStorage:{getItem:key=>session.get(key)||null},
 saved});
vm.runInContext(source,context);
vm.runInContext("localAction=async(store,mode,work)=>work({put:row=>saved.set(row.key,row),get:key=>saved.get(key)});",context);
await vm.runInContext("saveSnapshot('/me',{name:'owner'})",context);
assert.equal(saved.get('1001:/me').data.name,'owner');
session.set('pb_browser_session_only','1');
assert.equal(await vm.runInContext("readSnapshot('/me')",context),null);
vm.runInContext('setSnapshotUser(1003)',context);
await vm.runInContext("saveSnapshot('/me',{name:'staff'})",context);
assert.equal((await vm.runInContext("readSnapshot('/me')",context)).name,'staff');
assert.equal(saved.get('1001:/me').data.name,'owner');
vm.runInContext('setSnapshotUser(null)',context);
assert.equal(await vm.runInContext("readSnapshot('/me')",context),null);
session.delete('pb_browser_session_only');
assert.equal((await vm.runInContext("readSnapshot('/me')",context)).name,'owner');
console.log('PASS: browser-account cache ignores old Telegram identity and clears identity on switch');
