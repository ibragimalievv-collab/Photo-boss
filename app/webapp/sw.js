// Cache executable shell only. Authenticated API responses never enter CacheStorage.
const CACHE='photo-boss-shell-v1';
const SDK='https://telegram.org/js/telegram-web-app.js';
const STATIC=['/app/','/app/config.js','/app/css/styles.css','/app/assets/icon.svg',
 ...['app','api','localstore','outbox','workflow','workday','domain','icons','telegram','academy','practice','development','team','insights'].map(n=>`/app/js/${n}.js`),
 '/shift/attendance.js','/shift/attendance.css','/people/people.js','/people/people.css',
 ...['chat','calls','ringtone','recorder','ui','camera'].map(n=>`/work-chat/${n}.js`),'/work-chat/chat.css'];
self.addEventListener('install',event=>event.waitUntil((async()=>{const cache=await caches.open(CACHE);await Promise.all([...STATIC,SDK].map(async p=>{try{const response=await fetch(p,{cache:'reload',signal:AbortSignal.timeout(15000),...(p===SDK?{mode:'no-cors'}:{})});if(response.ok||response.type==='opaque')await cache.put(p,response);}catch{}}));await self.skipWaiting();})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const key of await caches.keys())if(key.startsWith('photo-boss-shell-')&&key!==CACHE)await caches.delete(key);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(event.request.method!=='GET'||(url.href!==SDK&&(url.origin!==self.location.origin||!STATIC.includes(url.pathname))))return;
 const key=url.href===SDK?SDK:url.pathname;
 event.respondWith((async()=>{try{const response=await fetch(event.request);if(response.ok||response.type==='opaque'){const cache=await caches.open(CACHE);await cache.put(key,response.clone());}return response;}catch(error){const cached=await caches.match(key);if(cached)return cached;throw error;}})());
});
