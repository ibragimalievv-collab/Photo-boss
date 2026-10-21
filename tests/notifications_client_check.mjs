import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const handlers={},notes=[],foreground=[],requests=[],status={textContent:''},storage=new Map();
let existing=null,unsubscribed=0,intervals=0,unsubscribeFailure=false;
const sub={endpoint:'https://web.push.apple.com/test-device',toJSON(){return {endpoint:this.endpoint,keys:{p256dh:'test',auth:'test'}};},async unsubscribe(){existing=null;unsubscribed++;}};
const worker={pushManager:{async getSubscription(){return existing;},async subscribe(opts){assert.equal(opts.userVisibleOnly,true);existing=sub;return sub;}}};
const notice={id:7,title:'Скоро съёмка',description:'13:00 · Hotel · номер 100',url:'/app/#shootings?booking=7',read:false,createdAt:'2026-10-20T09:40:00Z',pushState:'NO_DEVICE'};
const ctx=vm.createContext({Uint8Array,JSON,Date,URL,Promise,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},isSecureContext:true,atob:s=>Buffer.from(s,'base64').toString('binary'),
 api:async(path,opts)=>{requests.push({path,opts});if(path==='/push/unsubscribe'&&unsubscribeFailure)throw Error('Network unavailable');return {configured:true,channel:'app',publicKey:'BABC',activeDevices:0,items:[notice]};},esc:x=>String(x),
 Notification:{permission:'default',async requestPermission(){this.permission='granted';return 'granted';}},
 navigator:{userAgent:'Android',serviceWorker:{register:async()=>worker,ready:Promise.resolve(worker),getRegistration:async()=>worker}},
 window:{PushManager:class{},Notification:class{}},matchMedia:()=>({matches:true}),
 localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setInterval:()=>++intervals,clearInterval(){},
 document:{addEventListener:(kind,fn)=>handlers[kind]=fn,dispatchEvent:event=>foreground.push(event),querySelector:()=>status,querySelectorAll:()=>[]}});
const src=fs.readFileSync(new URL('../app/webapp/js/notifications.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replaceAll('export ','');vm.runInContext(src,ctx);
await vm.runInContext('syncPushAccount({user:{id:1}})',ctx);
const notificationHtml=await vm.runInContext('notificationsView()',ctx);
assert.match(notificationHtml,/Включить уведомления/);
assert.match(notificationHtml,/13:00 · Hotel · номер 100/);
assert.match(notificationHtml,/Нет подключённого устройства/);
notice.pushState='EXPIRED';
const expiredHtml=await vm.runInContext('notificationsView()',ctx);
assert.match(expiredHtml,/13:00 · Hotel · номер 100/);
assert.match(expiredHtml,/Время напоминания истекло; новые push не отправляются/);
notice.pushState='HISTORY_ONLY';
assert.match(await vm.runInContext('notificationsView()',ctx),/Устройство подключено позже события/);
await vm.runInContext('refreshNoticeFeed()',ctx);
for(const pushState of ['EXPIRED','SKIPPED']){notice.id++;notice.pushState=pushState;await vm.runInContext('refreshNoticeFeed()',ctx);}
assert.equal(foreground.length,0,'Returning to the app must not show stale reminder popups');
notice.id++;notice.pushState='NO_DEVICE';await vm.runInContext('refreshNoticeFeed()',ctx);
assert.equal(foreground.length,1,'A current inbox notice still appears without system push');
assert.equal(foreground[0].type,'pb-notification');
const click=async action=>handlers.click({target:{closest:()=>({dataset:{push:action},disabled:false})}});
await click('enable');assert(requests.some(r=>r.path==='/push/subscribe'));assert.match(status.textContent,/подписано/);
await click('test');assert(requests.some(r=>r.path==='/push/test'));assert.match(status.textContent,/ещё не подтверждение/);
await click('disable');assert.equal(unsubscribed,1);assert(requests.some(r=>r.path==='/push/unsubscribe'));assert.equal(storage.get('pb-push-disabled'),'1');
notice.id++;await vm.runInContext('refreshNoticeFeed()',ctx);
assert.equal(foreground.length,2,'Disabling phone push must preserve the in-app feed');
await click('enable');unsubscribeFailure=true;await click('disable');
assert.equal(unsubscribed,2,'Local device unsubscribe must still run after a network error');
assert.match(status.textContent,/Network unavailable/);
notice.id++;await vm.runInContext('refreshNoticeFeed()',ctx);
assert.equal(foreground.length,3,'A phone unsubscribe network error must not stop in-app notices');
const badge={hidden:true,textContent:''},button={label:'',querySelector:()=>badge,setAttribute(_key,value){this.label=value;}};
ctx.document.querySelector=selector=>selector.includes('app-notifications')?button:status;
vm.runInContext('state={configured:true,activeDevices:0,items:[{read:false}]};refreshNotificationBadge()',ctx);
assert.equal(button.label,'Уведомления: 1 новых');assert.equal(badge.textContent,'1');assert.equal(badge.hidden,false);
const swHandlers={};const sw=vm.createContext({URL,AbortSignal,self:{location:{origin:'https://pb.example'},addEventListener:(name,fn)=>swHandlers[name]=fn,registration:{showNotification:async(...args)=>notes.push(args)},clients:{matchAll:async()=>[],openWindow:async url=>requests.push({url})}}});
vm.runInContext(fs.readFileSync(new URL('../app/webapp/sw.js',import.meta.url),'utf8'),sw);
let promise;swHandlers.push({data:{json:()=>({title:'Photo Boss',url:'https://evil.example',body:'New event'})},waitUntil:p=>promise=p});await promise;
assert.equal(notes[0][1].data.url,'/app/#home');
for(const expiresAt of ['2000-01-01T00:00:00Z','invalid']){swHandlers.push({data:{json:()=>({title:'Скоро съёмка',expiresAt})},waitUntil:p=>promise=p});await promise;}
assert.equal(notes.length,1,'Expired reminders must not appear on the device');
swHandlers.push({data:{json:()=>({title:'Скоро съёмка',expiresAt:'2100-01-01T00:00:00Z'})},waitUntil:p=>promise=p});await promise;
assert.equal(notes.length,2,'A reminder before its deadline can still appear');
swHandlers.notificationclick({notification:{close(){},data:{url:'/app/#academy'}},waitUntil:p=>promise=p});await promise;
assert(requests.some(r=>r.url==='https://pb.example/app/#academy'));
console.log('PASS: permission click, subscription, account detach, queued test, reminder expiry without losing inbox details, system notification and safe click route');
