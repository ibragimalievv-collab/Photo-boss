// Cache executable shell only. Authenticated API responses never enter CacheStorage.
const STANDALONE=(()=>{try{return new URL(self.location.href||self.location.origin+'/app/sw.js').searchParams.get('standalone')==='1';}catch{return false;}})();
const CACHE=STANDALONE?'photo-boss-shell-v82-standalone':'photo-boss-shell-v82-ru-migration';
const SDK=STANDALONE?null:'https://telegram.org/js/telegram-web-app.js';
const STATIC=['/app/','/app/js/schedule-hours.js','/app/config.js','/app/manifest.webmanifest','/app/css/styles.css','/app/css/galleries.css','/app/css/staff-widget.css','/app/css/workspace-review.css','/app/css/schedule-month.css','/app/assets/icon.svg','/app/assets/widget-aperture.svg',
 ...['icon-192','icon-512','icon-maskable-512','apple-touch-icon'].map(n=>`/app/assets/${n}.png`),
 ...['schedule-month','workspace-overview','guest-phone','staff-widget','attendance-fines','hotel-points','storage-audit','disk-folder-audit','sale-disk','notifications','realtime-training','voice-pcm-worklet','booking-control','voice-training','form-state','academy-coach','academy-mentor','booking-diagnostic','delivery','galleries','gallery-folders','gallery-reserve','gallery-sale','gallery-view','gallery-client','app','api','browser-login','account','schedule-editor','schedule-import','install','localstore','outbox','workflow','sale-files','workday','lunch','hotel-pricing','bank-payments','merchant-qr','receipt-review','receipt-state','receipt-files','booking-receipt','domain','icons','telegram','academy','practice','development','team','insights'].map(n=>`/app/js/${n}.js`),
 '/shift/attendance.js','/shift/attendance.css','/people/people.js','/people/people.css',
 '/staff-login/scan.js','/staff-login/scan.css','/staff-login/decoder.js',
 ...['chat','calls','ringtone','recorder','ui','camera'].map(n=>`/work-chat/${n}.js`),'/work-chat/chat.css'];
self.addEventListener('install',event=>event.waitUntil((async()=>{
 const cache=await caches.open(CACHE),missing=[];
 await Promise.all([...STATIC,...(SDK?[SDK]:[])].map(async path=>{
  try{const response=await fetch(path,{cache:'reload',signal:AbortSignal.timeout(15000),...(path===SDK?{mode:'no-cors'}:{})});if(!response.ok&&response.type!=='opaque')throw Error('asset unavailable');await cache.put(path,response);}
  catch{if(STATIC.includes(path))missing.push(path);}
 }));
 // A broken connection during an update must keep the previous complete shell.
 // Business records and attachments live in IndexedDB and are never touched.
 if(missing.length){await caches.delete(CACHE);throw Error('Offline update is incomplete');}
 await self.skipWaiting();
})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys())if(key.startsWith('photo-boss-shell-')&&key!==CACHE)await caches.delete(key);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(event.request.mode==='navigate'&&url.origin===self.location.origin&&url.pathname==='/app/') {
  // The public shell is safe to open without a network round trip. A complete
  // worker installation updates it; authenticated API data is never cached here.
  event.respondWith((async()=>await caches.match('/app/')||await fetch(event.request))());return;
 }
 if(event.request.method!=='GET'||(url.href!==SDK&&(url.origin!==self.location.origin||!STATIC.includes(url.pathname))))return;
 const key=url.href===SDK?SDK:url.pathname;
 event.respondWith((async()=>{try{const response=await fetch(event.request);if(response.ok||response.type==='opaque'){const cache=await caches.open(CACHE);await cache.put(key,response.clone());}return response;}catch(error){const cached=await caches.match(key);if(cached)return cached;throw error;}})());
});

self.addEventListener('push',event=>event.waitUntil((async()=>{
 let data={};try{data=event.data?.json()||{};}catch{}
 if(data.expiresAt!==undefined){const expires=typeof data.expiresAt==='string'?Date.parse(data.expiresAt):NaN;if(!Number.isFinite(expires)||Date.now()>=expires)return;}
 const title=typeof data.title==='string'?data.title.slice(0,120):'Photo Boss';
 let url='/app/#home';try{const target=new URL(data.url,self.location.origin);if(target.origin===self.location.origin&&target.pathname==='/app/')url=target.pathname+target.hash;}catch{}
 await self.registration.showNotification(title,{body:typeof data.body==='string'?data.body.slice(0,250):'Новое событие в Photo Boss.',
  icon:'/app/assets/icon-192.png',badge:'/app/assets/icon-192.png',tag:data.tag||'photo-boss',data:{url}});
})()));
self.addEventListener('notificationclick',event=>{
 event.notification.close();event.waitUntil((async()=>{
  const url=new URL(event.notification.data?.url||'/app/#home',self.location.origin);
  if(url.origin!==self.location.origin||url.pathname!=='/app/')return;
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  for(const client of windows){if(new URL(client.url).origin===url.origin&&new URL(client.url).pathname==='/app/'){await client.navigate(url.href);return client.focus();}}
  return self.clients.openWindow(url.href);
 })());
});

