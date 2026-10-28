import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync('app/webapp/js/bank-payments.js','utf8').replace(/^import .*;\n/gm,'').replaceAll('export ','');
function harness(){
 const listeners={},calls=[],status={textContent:''},button={disabled:false};
 let panel={querySelector:()=>status},respond=async()=>({environment:'sandbox',enabled:false,authenticationConfigurationComplete:false,orderConfigurationComplete:false});
 button.closest=selector=>selector==='[data-bank-connection]'?button:selector==='#bankConnectionPanel'?panel:null;
 const context=vm.createContext({Set,Map,Number,JSON,Date,Error,
  api:async(path,options)=>{calls.push({path,options});return respond(path,options);},
  document:{addEventListener:(name,fn)=>(listeners[name]??=[]).push(fn),querySelector:selector=>selector==='#bankConnectionPanel'?panel:null},
  window:{addEventListener(){}},
 });
 vm.runInContext(source,context);
 const render=allowed=>{context.allowed=allowed;return vm.runInContext('bankConnectionPanel({canCheckBankConnection:allowed})',context);};
 return {calls,status,button,render,respond:fn=>{respond=fn;},click:async()=>{for(const fn of listeners.click)await fn({target:button});},
  replace:()=>{panel={querySelector:()=>status};render(true);}};
}
{
 const h=harness();assert.equal(h.render(false),'');assert.match(h.render(true),/Проверить подключение Сбера/);
 await h.click();assert.deepEqual(h.calls.map(c=>c.path),['/bank-payments/connection']);
 assert.equal(h.calls[0].options.allowSnapshot,false);assert.match(h.status.textContent,/параметры банка/);assert.match(h.status.textContent,/выключен/);
}
{
 const h=harness();h.render(true);h.respond(async path=>({environment:'sandbox',enabled:false,authenticationConfigurationComplete:true,orderConfigurationComplete:false,
  certificate:{checked:path.endsWith('/check'),valid:true},oauth:{authenticated:path.endsWith('/check')}}));
 await h.click();assert.deepEqual(h.calls.map(c=>c.path),['/bank-payments/connection','/bank-payments/connection/check']);
 assert.equal(h.calls[1].options.method,'POST');assert.equal(JSON.stringify(h.calls[1].options.body),'{}');
 assert.match(h.status.textContent,/Сертификат проверен/);assert.match(h.status.textContent,/Сбер подтвердил доступ/);assert.match(h.status.textContent,/выключен/);
 assert.equal(h.button.disabled,false);
}
{
 const h=harness();h.render(true);let answer;h.respond(()=>new Promise(resolve=>{answer=resolve;}));
 const pending=h.click();await Promise.resolve();await h.click();assert.equal(h.calls.length,1);
 h.replace();h.status.textContent='Новый аккаунт';answer({authenticationConfigurationComplete:true});await pending;
 assert.equal(h.status.textContent,'Новый аккаунт');assert.equal(h.calls.length,1,'A stale page cannot start OAuth');
}
console.log('PASS: owner connection UI checks fresh settings and OAuth only; incomplete setup, double click and stale pages cannot create bank orders');
