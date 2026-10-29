// Cache executable shell only. Authenticated API responses never enter CacheStorage.
const CACHE='photo-boss-shell-v66-workspace-notifications';
const SDK='https://telegram.org/js/telegram-web-app.js';
const STATIC=['/app/','/app/config.js','/app/manifest.webmanifest','/app/css/styles.css','/app/css/galleries.css','/app/assets/icon.svg',
 ...['icon-192','icon-512','icon-maskable-512','apple-touch-icon'].map(n=>`/app/assets/${n}.png`),
 ...['attendance-fines','hotel-points','storage-audit','disk-folder-audit','sale-disk','notifications','realtime-training','booking-control','voice-training','form-state','academy-coach','academy-mentor','booking-diagnostic','delivery','galleries','gallery-folders','gallery-view','gallery-client','app','api','browser-login','account','schedule-editor','schedule-import','install','localstore','outbox','workflow','sale-files','workday','lunch','hotel-pricing','bank-payments','domain','icons','telegram','academy','practice','development','team','insights'].map(n=>`/app/js/${n}.js`),
 '/shift/attendance.js','/shift/attendance.css','/people/people.js','/people/people.css',
 ...['chat','calls','ringtone','recorder','ui','camera'].map(n=>`/work-chat/${n}.js`),'/work-chat/chat.css'];
self.addEventListener('install',event=>event.waitUntil((async()=>{const cache=await caches.open(CACHE);await Promise.all([...STATIC,SDK].map(async p=>{try{const response=await fetch(p,{cache:'reload',signal:AbortSignal.timeout(15000),...(p===SDK?{mode:'no-cors'}:{})});if(response.ok||response.type==='opaque')await cache.put(p,response);}catch{}}));await self.skipWaiting();})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys())if(key.startsWith('photo-boss-shell-')&&key!==CACHE)await caches.delete(key);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(event.request.mode==='navigate'&&url.origin===self.location.origin&&url.pathname==='/app/') {event.respondWith(fetch(event.request).catch(async()=>{const cached=await caches.match('/app/');return cached||Response.error();}));return;}
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

