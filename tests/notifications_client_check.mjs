import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const handlers={},notes=[],foreground=[],requests=[],status={textContent:''},storage=new Map();
let existing=null,unsubscribed=0,intervals=0,unsubscribeFailure=false;
const sub={endpoint:'https://web.push.apple.com/test-device',toJSON(){return {endpoint:this.endpoint,keys:{p256dh:'test',auth:'test'}};},async unsubscribe(){existing=null;unsubscribed++;}};
const worker={pushManager:{async getSubscription(){return existing;},async subscribe(opts){assert.equal(opts.userVisibleOnly,true);existing=sub;return sub;}}};
const notice={id:7,title:'Скоро съёмка',description:'13:00 · Hotel · номер 100',url:'/app/#shootings?booking=7',read:false,createdAt:'2026-10-20T09:40:00Z',pushState:'NO_DEVICE'};
const ctx=vm.createContext({Uint8Array,JSON,Date,URL,location:{origin:'https://pb.example'},Promise,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}},isSecureContext:true,atob:s=>Buffer.from(s,'base64').toString('binary'),
 api:async(path,opts)=>{requests.push({path,opts});if(path==='/push/unsubscribe'&&unsubscribeFailure)throw Error('Network unavailable');return {configured:true,channel:'app',publicKey:'BABC',activeDevices:0,items:[notice],unreadCount:notice.read?0:1,hasMore:false,olderCursor:null,latestId:notice.id};},esc:x=>String(x),
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
assert.equal(vm.runInContext("notificationActionText({kind:'attendance_fine',url:'/app/#more?attendanceFine=attendance%3Afine%3A2026-10-21%3A2%3Alate'})",ctx),'Проверить и исправить начисление');
assert.equal(vm.runInContext("notificationActionText({kind:'attendance_fine',url:'/app/#finance'})",ctx),'Посмотреть мои начисления');
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
vm.runInContext('state={configured:true,activeDevices:0,unreadCount:1,items:[{read:false}]};refreshNotificationBadge()',ctx);
assert.equal(button.label,'Уведомления. Непрочитанных: 1');assert.equal(badge.textContent,'1');assert.equal(badge.hidden,false);
const originalApi=ctx.api,beforeOpen=foreground.length;let finishRead;
ctx.api=async(path,opts)=>path==='/push/read'?new Promise(resolve=>{finishRead=resolve;}):originalApi(path,opts);
const opening=handlers.click({preventDefault(){},target:{closest:()=>({dataset:{noticeRead:'7'},getAttribute:()=>'/app/#more?attendanceFine=attendance%3Afine%3A2026-10-21%3A2%3Alate'})}});
assert.equal(foreground.length,beforeOpen+1,'Open the target before waiting for the read acknowledgement');
assert.equal(foreground.at(-1).type,'pb-notification-open');assert.match(foreground.at(-1).detail.url,/attendanceFine=/);
await new Promise(setImmediate);finishRead({ok:true});await opening;assert.equal(foreground.length,beforeOpen+1,'A late read reply must not close the newly opened target');ctx.api=originalApi;
const fineCtx=vm.createContext({Date,api:async()=>({canEditRules:false,items:[{userId:2,date:'2026-10-21',kind:'late',name:'Photo',label:'Опоздание',amount:500,calculatedAmount:500,ruleConfigured:true}]}),esc:x=>String(x),document:{addEventListener(){}}});
vm.runInContext(fs.readFileSync(new URL('../app/webapp/js/attendance-fines.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'').replaceAll('export ',''),fineCtx);
assert.match(await vm.runInContext("attendanceFinesView('attendance:fine:2026-10-21:2:late')",fineCtx),/<details class="panel section" open>/);
assert.doesNotMatch(await vm.runInContext("attendanceFinesView('unrelated-key')",fineCtx),/<details class="panel section" open>/);
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
const ownerClick=async action=>handlers.click({target:{closest:selector=>selector==='[data-owner-telegram]'?{dataset:{ownerTelegram:action},disabled:false}:null}});
vm.runInContext('state.ownerTelegram=false',ctx);
await ownerClick('toggle');
const ownPreference=requests.findLast(r=>r.path==='/push/owner-telegram');
assert.deepEqual(JSON.parse(JSON.stringify(ownPreference.opts.body)),{enabled:true});
assert.equal(vm.runInContext('state.ownerTelegram',ctx),true);
await ownerClick('test');assert.match(status.textContent,/Статус отправки Telegram/);
await ownerClick('toggle');assert.equal(vm.runInContext('state.ownerTelegram',ctx),false);
console.log('PASS: permission click, subscription, account detach, owner-only Telegram toggle/test, reminder expiry without losing inbox details, system notification and safe click route');

const merged=vm.runInContext(`mergeNotificationItems(
 [{id:8,eventKey:'same',title:'Important',read:false},{id:9,eventKey:'distinct-a',title:'Same text',read:false}],
 [{id:8,eventKey:'same',title:'Important',read:true},{id:10,eventKey:'distinct-b',title:'Same text',read:false},{id:10,eventKey:'distinct-b',title:'Same text',read:false}])`,ctx);
assert.deepEqual(Array.from(merged,row=>row.id),[10,9,8]);
assert.equal(merged.find(row=>row.id===8).read,true,'a current acknowledged copy replaces the stale copy');
assert.equal(merged.filter(row=>row.title==='Same text').length,2,'identical text does not merge separate events');
vm.runInContext('state={unreadCount:137,items:[{id:1,read:false}]};refreshNotificationBadge()',ctx);
assert.equal(badge.textContent,'137','badge uses whole eligible history, never the loaded page length');
let staleReply;
ctx.api=async()=>new Promise(resolve=>{staleReply=resolve;});
const staleFeed=vm.runInContext('refreshNoticeFeed()',ctx);
vm.runInContext('mutationVersion++;state={unreadCount:0,items:[{id:1,read:true}]};',ctx);
staleReply({items:[{id:1,read:false}],unreadCount:1,hasMore:false,latestId:1});await staleFeed;
assert.equal(vm.runInContext('state.unreadCount',ctx),0,'an older GET cannot resurrect an unread item after a read acknowledgement');
const oldAccount=vm.runInContext('refreshNoticeFeed()',ctx);
vm.runInContext('revision++;account=2;state=null;',ctx);
staleReply({items:[{id:99,read:false}],unreadCount:1,hasMore:false,latestId:99});await oldAccount;
assert.equal(vm.runInContext('state',ctx),null,'an old account response cannot populate another inbox');
ctx.api=originalApi;
console.log('PASS: whole-history unread count, stable event dedup, distinct equal-text events, stale read/account responses ignored');
