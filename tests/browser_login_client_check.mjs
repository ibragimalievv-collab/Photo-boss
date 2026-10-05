import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/browser-login.js',import.meta.url),'utf8')
 .replace("import {esc} from './domain.js';",'const esc=s=>String(s);').replaceAll('export ','');

function scenario(responses){
 const app={innerHTML:''},status={textContent:''},requests=[],events={},timers=new Map();let clock=0;
 const context=vm.createContext({URL,Date,Error,AbortController,console,
  document:{hidden:false,querySelector:s=>s==='#app'?app:status,addEventListener:(name,fn)=>events[name]=fn},
  window:{addEventListener:(name,fn)=>events[name]=fn},
  setTimeout:fn=>{const id=++clock;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,options)=>{requests.push({url,options});const next=responses.shift();if(next instanceof Error)throw next;return {ok:!next.error,status:next.error?next.status:200,json:async()=>next};}});
 vm.runInContext(source,context);
 return {context,app,status,requests,events,timers,run:code=>vm.runInContext(code,context)};
}
const begin={telegramUrl:'https://t.me/Really_Boss_bot?start=web_abcdefghijklmnopqrstuvwx',code:'123456',expiresAt:Math.floor(Date.now()/1000)+300};
const good=scenario([begin,{status:'pending'},{status:'authenticated'}]);
good.context.logins=0;
await good.run('startBrowserLogin(()=>{logins++})');
assert.match(good.app.innerHTML,/123456/);
assert.match(good.app.innerHTML,/target="_blank"/);
await good.run('checkBrowserLogin()');assert.equal(good.context.logins,0);
await good.run('checkBrowserLogin()');assert.equal(good.context.logins,1);
assert.equal(good.timers.size,0);
for(const {options} of good.requests){assert.equal(options.credentials,'same-origin');assert.equal(options.method,'POST');assert.equal(options.headers['X-PhotoBoss-Session'],'1');}
const rejected=scenario([begin,{error:'Вход отменён',status:403}]);
await rejected.run('startBrowserLogin(()=>{throw new Error("Unexpected login")})');
await rejected.run('checkBrowserLogin()');assert.equal(rejected.status.textContent,'Вход отменён');assert.equal(rejected.timers.size,0);
const retry=scenario([begin,new Error('Network interruption'),{status:'authenticated'}]);retry.context.logins=0;
await retry.run('startBrowserLogin(()=>{logins++})');await retry.run('checkBrowserLogin()');assert.equal(retry.timers.size,1);
await retry.events.focus();assert.equal(retry.context.logins,1);
const expired=scenario([{...begin,expiresAt:1}]);
await expired.run('startBrowserLogin(()=>{throw new Error("Unexpected login")})');await expired.run('checkBrowserLogin()');assert.equal(expired.requests.length,1);assert.match(expired.status.textContent,/истекло/);
console.log('PASS: browser login link and code, pending approval, cookie requests, cancellation, retry on focus, expiry');
