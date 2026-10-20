import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const handlers={},notes=[],requests=[],status={textContent:''},storage=new Map();
let existing=null,unsubscribed=0;
const sub={endpoint:'https://web.push.apple.com/test-device',toJSON(){return {endpoint:this.endpoint,keys:{p256dh:'test',auth:'test'}};},async unsubscribe(){existing=null;unsubscribed++;}};
const worker={pushManager:{async getSubscription(){return existing;},async subscribe(opts){assert.equal(opts.userVisibleOnly,true);existing=sub;return sub;}}};
const ctx=vm.createContext({Uint8Array,JSON,Date,URL,Promise,isSecureContext:true,atob:s=>Buffer.from(s,'base64').toString('binary'),
 api:async(path,opts)=>{requests.push({path,opts});return {configured:true,channel:'app',publicKey:'BABC',activeDevices:0,items:[{id:7,title:'Скоро съёмка',description:'13:00 · Hotel · номер 100',url:'/app/#shootings?booking=7',read:false,createdAt:'2026-10-20T09:40:00Z',pushState:'NO_DEVICE'}]};},esc:x=>String(x),
 Notification:{permission:'default',async requestPermission(){this.permission='granted';return 'granted';}},
 navigator:{userAgent:'Android',serviceWorker:{register:async()=>worker,ready:Promise.resolve(worker),getRegistration:async()=>worker}},
 window:{PushManager:class{},Notification:class{}},matchMedia:()=>({matches:true}),
 localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 document:{addEventListener:(kind,fn)=>handlers[kind]=fn,querySelector:()=>status,querySelectorAll:()=>[]}});
const src=fs.readFileSync(new URL('../app/webapp/js/notifications.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replaceAll('export ','');vm.runInContext(src,ctx);
await vm.runInContext('syncPushAccount({user:{id:1}})',ctx);
const notificationHtml=await vm.runInContext('notificationsView()',ctx);
assert.match(notificationHtml,/Включить уведомления/);
assert.match(notificationHtml,/13:00 · Hotel · номер 100/);
assert.match(notificationHtml,/Нет подключённого устройства/);
const click=async action=>handlers.click({target:{closest:()=>({dataset:{push:action},disabled:false})}});
await click('enable');assert(requests.some(r=>r.path==='/push/subscribe'));assert.match(status.textContent,/подписано/);
await click('test');assert(requests.some(r=>r.path==='/push/test'));assert.match(status.textContent,/ещё не подтверждение/);
await click('disable');assert.equal(unsubscribed,1);assert(requests.some(r=>r.path==='/push/unsubscribe'));assert.equal(storage.get('pb-push-disabled'),'1');
const swHandlers={};const sw=vm.createContext({URL,AbortSignal,self:{location:{origin:'https://pb.example'},addEventListener:(name,fn)=>swHandlers[name]=fn,registration:{showNotification:async(...args)=>notes.push(args)},clients:{matchAll:async()=>[],openWindow:async url=>requests.push({url})}}});
vm.runInContext(fs.readFileSync(new URL('../app/webapp/sw.js',import.meta.url),'utf8'),sw);
let promise;swHandlers.push({data:{json:()=>({title:'Photo Boss',url:'https://evil.example',body:'New event'})},waitUntil:p=>promise=p});await promise;
assert.equal(notes[0][1].data.url,'/app/#home');
swHandlers.notificationclick({notification:{close(){},data:{url:'/app/#academy'}},waitUntil:p=>promise=p});await promise;
assert(requests.some(r=>r.url==='https://pb.example/app/#academy'));
console.log('PASS: permission click, subscription, account detach, queued test, system notification and safe click route');
