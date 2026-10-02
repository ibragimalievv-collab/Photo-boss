import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/api.js',import.meta.url),'utf8')
 .replace("import {readSnapshot,saveSnapshot} from './localstore.js';",'const readSnapshot=async()=>null,saveSnapshot=async()=>{};').replaceAll('export ','');
const fresh=`auth_date=${Math.floor(Date.now()/1000)}&user=${encodeURIComponent('{"id":1001}')}&hash=${'a'.repeat(64)}`;
async function scenario({hash,live='',status=200,afterLoadHash}){
 const storage=new Map(),sent=[];
 const context=vm.createContext({URL,URLSearchParams,FormData,AbortController,Date,Number,Promise,setTimeout,clearTimeout,atob,
  location:{hash,origin:'https://photo-boss.onrender.com'},
  window:{Telegram:{WebApp:{initData:live}},dispatchEvent(){}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  fetch:async(url,options)=>{sent.push(options);return {status,ok:status===200,json:async()=>status===200?{user:{id:1}}:{error:'Invalid signature'}};}});
 vm.runInContext(source,context);
 if(afterLoadHash!==undefined)context.location.hash=afterLoadHash;
 try{await vm.runInContext("api('/me')",context);}catch(error){assert.equal(error.status,status);}
 return {storage,sent};
}
for(const prefix of ['#','#home?']){
 const r=await scenario({hash:prefix+'tgWebAppData='+encodeURIComponent(fresh),afterLoadHash:'#home'});
 assert.equal(r.sent.length,1);
 assert.equal(r.sent[0].headers['X-Telegram-Init-Data'],fresh);
 assert.equal(r.storage.get('pb_init_data_v1'),fresh);
}
const rejected=await scenario({hash:'#tgWebAppData='+encodeURIComponent(fresh),status:401});
assert.equal(rejected.sent.length,1);
assert.equal(rejected.storage.has('pb_init_data_v1'),false);
const preferred=await scenario({hash:'#tgWebAppData='+encodeURIComponent(fresh),live:fresh+'&query_id=live'});
assert.equal(preferred.sent[0].headers['X-Telegram-Init-Data'],fresh+'&query_id=live');
console.log('PASS: SDK missing, both launch formats, route change, server rejection, live precedence');

const savedSession=await scenario({hash:'#home'});
assert.equal(savedSession.sent.length,1);
assert.equal(savedSession.sent[0].headers['X-PhotoBoss-Session'],'1');
assert.equal(savedSession.sent[0].credentials,'same-origin');
assert.equal(savedSession.sent[0].headers['X-Telegram-Init-Data'],undefined);
const stale=fresh.replace(/auth_date=\d+/,`auth_date=${Math.floor(Date.now()/1000)-3*86400}`);
const staleSession=await scenario({hash:'#home',live:stale});
assert.equal(staleSession.sent.length,1);
assert.equal(staleSession.sent[0].headers['X-Telegram-Init-Data'],undefined);
console.log('PASS: no launch data and expired Telegram data use the protected server session');
