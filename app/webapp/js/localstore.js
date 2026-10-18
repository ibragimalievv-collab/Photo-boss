// Personal snapshots and queued blobs share one versioned local database.
export const openLocal=()=>new Promise((resolve,reject)=>{
 const r=indexedDB.open('photo-boss-outbox',2);
 r.onupgradeneeded=()=>{for(const name of ['operations','snapshots'])if(!r.result.objectStoreNames.contains(name))r.result.createObjectStore(name,{keyPath:'key'});};
 r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
});
export async function localAction(store,mode,work){const db=await openLocal();try{return await new Promise((resolve,reject)=>{const tx=db.transaction(store,mode);let req;tx.oncomplete=()=>resolve(req?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('Не удалось сохранить данные на устройстве. Освободите место и повторите.'));try{req=work(tx.objectStore(store));}catch(error){tx.abort();reject(error);}});}finally{db.close();}}
let verifiedIdentity=null,identityCleared=false;
const LAST_ID='pb-offline-identity-v1';
export function setSnapshotUser(id){verifiedIdentity=Number.isSafeInteger(id)&&id>0?id:null;identityCleared=!verifiedIdentity;try{if(verifiedIdentity)localStorage.setItem(LAST_ID,String(verifiedIdentity));else localStorage.removeItem(LAST_ID);}catch{}}
function identity(){
 if(verifiedIdentity)return verifiedIdentity;
 if(identityCleared){try{if(sessionStorage.getItem('pb_browser_session_only')==='1')return null;}catch{}}
 try{const id=Number(localStorage.getItem(LAST_ID));if(!identityCleared&&Number.isSafeInteger(id)&&id>0)return id;}catch{}
 try{if(sessionStorage.getItem('pb_browser_session_only')==='1')return null;}catch{}
 try{return JSON.parse(new URLSearchParams(window.Telegram?.WebApp?.initData||'').get('user')||'null')?.id||null;}catch{return null;}
}
const cachedPaths=new Set(['/me','/workflow','/workday','/attendance','/onboarding','/dashboard','/shoot-control','/galleries','/academy','/academy/coach?track=photographer','/academy/coach?track=booking','/academy/mentor?track=photographer','/academy/mentor?track=booking','/academy/diagnostic']);
function cacheable(path){return cachedPaths.has(path)||/^\/workflow\?booking=\d+$/.test(path)||/^\/bookings\?day=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/schedule\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/shoot-control\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/delivery\/\d+$/.test(path);}
export async function saveSnapshot(path,data){const id=identity();if(id&&cacheable(path))await localAction('snapshots','readwrite',s=>s.put({key:`${id}:${path}`,data,at:Date.now()}));}
export async function readSnapshot(path){const id=identity();if(!id||!cacheable(path))return null;const row=await localAction('snapshots','readonly',s=>s.get(`${id}:${path}`));if(!row||Date.now()-row.at>7*86400000)return null;return {...row.data,_offline:true,_cachedAt:row.at};}

// A confirmed server reset invalidates only operational data owned by this
// account. Keep login, chat, Academy and other accounts' saved files intact.
export async function reconcileOperationsGeneration(userId,telegramId,generation){
 if(!Number.isSafeInteger(userId)||userId<1||!Number.isSafeInteger(telegramId)||telegramId<1||typeof generation!=='string')return false;
 const key=`operations-generation:${userId}`,db=await openLocal();
 try{return await new Promise((resolve,reject)=>{
  const tx=db.transaction(['operations','snapshots'],'readwrite'),snapshots=tx.objectStore('snapshots');let changed=false;
  const marker=snapshots.get(key);
  marker.onsuccess=()=>{
   const previous=marker.result;
   changed=previous?previous.generation!==generation:Boolean(generation);
   if(changed){
    const operations=tx.objectStore('operations').openCursor();
    operations.onsuccess=()=>{const cursor=operations.result;if(!cursor)return;if(cursor.value.userId===userId&&cursor.value.kind!=='cash_expense')cursor.delete();cursor.continue();};
    const cached=snapshots.openCursor(),prefix=`${telegramId}:`;
    cached.onsuccess=()=>{const cursor=cached.result;if(!cursor)return;const row=cursor.value,path=String(row.key).startsWith(prefix)?String(row.key).slice(prefix.length):null;if(path&&path!=='/me'&&!path.startsWith('/academy'))cursor.delete();cursor.continue();};
   }
   snapshots.put({key,generation,at:Date.now()});
  };
  tx.oncomplete=()=>resolve(changed);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('Не удалось обновить рабочие данные на устройстве.'));
 });}finally{db.close();}
}

// A queued write can start before reset and acquire its IndexedDB transaction
// afterwards. Check the durable marker inside that same transaction so even a
// late "syncing" status cannot resurrect a row the reset has already purged.
export async function writeOperations(rows,{add=false,addKeys=[]}={}){
 if(!rows.length)return true;
 const actor=rows[0].userId;
 if(rows.some(row=>row.userId!==actor))throw new Error('Операции разных аккаунтов нельзя сохранять вместе.');
 const db=await openLocal();
 try{return await new Promise((resolve,reject)=>{
  const tx=db.transaction(['operations','snapshots'],'readwrite'),store=tx.objectStore('operations');let accepted=false;
  const marker=tx.objectStore('snapshots').get(`operations-generation:${actor}`);
  marker.onsuccess=()=>{
   const generation=marker.result?.generation||'';
   accepted=rows.every(row=>row.kind==='cash_expense'||(row.generation||'')===generation);
   if(accepted)for(const row of rows)store[add||addKeys.includes(row.key)?'add':'put'](row);
  };
  tx.oncomplete=()=>{if(!accepted)window.dispatchEvent(new Event('pb-operations-stale'));resolve(accepted);};
  tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('Не удалось сохранить рабочие операции.'));
 });}finally{db.close();}
}
