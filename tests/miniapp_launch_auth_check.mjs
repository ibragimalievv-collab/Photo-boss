import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/webapp/js/api.js',import.meta.url),'utf8')
 .replace("import {readSnapshot,saveSnapshot,setSnapshotUser,reconcileOperationsGeneration} from './localstore.js';",'const readSnapshot=async()=>null,saveSnapshot=async()=>{},setSnapshotUser=()=>{},reconcileOperationsGeneration=async()=>false;').replaceAll('export ','');
const fresh=`auth_date=${Math.floor(Date.now()/1000)}&user=${encodeURIComponent('{"id":1001}')}&hash=${'a'.repeat(64)}`;
async function scenario({hash,live='',status=200,afterLoadHash,options={},browserLogin=false}){
 const storage=new Map(),sent=[];
 const context=vm.createContext({URL,URLSearchParams,FormData,AbortController,Date,Number,Promise,setTimeout,clearTimeout,atob,
  callOptions:options,
  location:{hash,origin:'https://photo-boss.onrender.com'},
  window:{Telegram:{WebApp:{initData:live}},dispatchEvent(){}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  fetch:async(url,options)=>{sent.push(options);return {status,ok:status===200,json:async()=>status===200?{user:{id:1}}:{error:'Invalid signature'}};}});
 vm.runInContext(source,context);
 if(browserLogin)vm.runInContext('preferBrowserSession()',context);
 if(afterLoadHash!==undefined)context.location.hash=afterLoadHash;
 let result;
 try{result=await vm.runInContext("api('/me',callOptions)",context);}catch(error){assert.equal(error.status,status);}
 return {storage,sent,result};
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

const upload=new FormData();upload.append('file',new Blob(['sample']),'test.txt');
const media=await scenario({hash:'#home',live:fresh,options:{method:'POST',body:upload,responseType:'response'}});
assert.equal(media.result.status,200);
assert.equal(media.sent[0].body,upload);
assert.equal(media.sent[0].headers['Content-Type'],undefined);
console.log('PASS: multipart media shares authentication and returns the unconsumed response');

const browser=await scenario({hash:'#home',live:fresh,browserLogin:true});
assert.equal(browser.sent[0].headers['X-Telegram-Init-Data'],undefined);
assert.equal(browser.sent[0].headers['X-PhotoBoss-Owner-Launch'],undefined);
assert.equal(browser.sent[0].headers['X-PhotoBoss-Session'],'1');
console.log('PASS: confirmed browser login uses the new session instead of old launch credentials');

async function rosterScenario({online,allowSnapshot}){
 let fetches=0,error,result;
 const context=vm.createContext({URL,URLSearchParams,FormData,AbortController,Date,Number,Promise,setTimeout,clearTimeout,atob,
  allowSnapshot,navigator:{onLine:online},location:{hash:'#home',origin:'https://photo-boss.onrender.com'},
  window:{Telegram:{WebApp:{initData:fresh}},dispatchEvent(){}},
  sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(url,options)=>{fetches++;assert.equal(options.allowSnapshot,undefined);throw new TypeError('Network unavailable');}});
 vm.runInContext(source.replace('const readSnapshot=async()=>null,','const readSnapshot=async()=>({marker:"old roster"}),'),context);
 try{result=await vm.runInContext("api('/schedule',{allowSnapshot})",context);}catch(caught){error=caught;}
 return {result,error,fetches};
}
for(const online of [false,true]){
 const cached=await rosterScenario({online,allowSnapshot:true});
 assert.equal(cached.result.marker,'old roster');
 const current=await rosterScenario({online,allowSnapshot:false});
 assert.equal(current.result,undefined);
 assert.equal(current.error.name,'Error');
 assert.equal(current.error.status,0);
 assert.equal(current.fetches,online?1:0);
}
console.log('PASS: schedule editor requires a fresh employee roster during offline and network failure');

// A second tab may already have purged IndexedDB. A changed in-memory token
// must still discard this tab's old forms and apply the server's new header.
{
 const sent=[],events=[];let generation='before',reject=false;
 const context=vm.createContext({URL,URLSearchParams,FormData,AbortController,Date,Number,Promise,setTimeout,clearTimeout,atob,Event,
  CustomEvent:class extends Event{constructor(type,options){super(type);this.detail=options.detail;}},
  location:{hash:'#home',origin:'https://photo-boss.onrender.com'},
  window:{Telegram:{WebApp:{initData:fresh}},dispatchEvent:event=>events.push(event)},
  sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  fetch:async(url,options)=>{sent.push(options);return {status:reject?409:200,ok:!reject,headers:{get:()=>null},json:async()=>reject?{error:'Рабочие данные обнулены.',code:'operations_reset'}:{user:{id:1,telegramId:1001},operationsGeneration:generation}};}});
 vm.runInContext(source,context);
 await vm.runInContext("api('/me')",context);assert.equal(events.length,0);
 generation='after';await vm.runInContext("api('/me')",context);
 assert.equal(events.length,1);assert.equal(events[0].type,'pb-operations-reset');assert.equal(events[0].detail.generation,'after');
 await vm.runInContext("api('/bookings',{method:'POST',body:{}})",context);
 assert.equal(sent.at(-1).headers['X-PhotoBoss-Operations-Generation'],'after');
 reject=true;await assert.rejects(vm.runInContext("api('/bookings',{method:'POST',body:{}})",context),error=>error.status===409);
 assert.equal(events.at(-1).type,'pb-operations-stale');
}
console.log('PASS: changed token in an already-reconciled tab reloads old UI, direct requests carry generation and server reset requests refresh identity');
