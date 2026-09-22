// Personal snapshots and queued blobs share one versioned local database.
export const openLocal=()=>new Promise((resolve,reject)=>{
 const r=indexedDB.open('photo-boss-outbox',2);
 r.onupgradeneeded=()=>{for(const name of ['operations','snapshots'])if(!r.result.objectStoreNames.contains(name))r.result.createObjectStore(name,{keyPath:'key'});};
 r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
});
export async function localAction(store,mode,work){const db=await openLocal();try{return await new Promise((resolve,reject)=>{const tx=db.transaction(store,mode);let req;tx.oncomplete=()=>resolve(req?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('Не удалось сохранить данные на устройстве. Освободите место и повторите.'));try{req=work(tx.objectStore(store));}catch(error){tx.abort();reject(error);}});}finally{db.close();}}
function identity(){try{return JSON.parse(new URLSearchParams(window.Telegram?.WebApp?.initData||'').get('user')||'null')?.id||null;}catch{return null;}}
const cachedPaths=new Set(['/me','/workflow','/workday','/attendance','/onboarding']);
export async function saveSnapshot(path,data){const id=identity();if(id&&cachedPaths.has(path))await localAction('snapshots','readwrite',s=>s.put({key:`${id}:${path}`,data,at:Date.now()}));}
export async function readSnapshot(path){const id=identity();if(!id||!cachedPaths.has(path))return null;const row=await localAction('snapshots','readonly',s=>s.get(`${id}:${path}`));if(!row||Date.now()-row.at>7*86400000)return null;return {...row.data,_offline:true,_cachedAt:row.at};}
