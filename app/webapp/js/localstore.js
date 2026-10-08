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
const cachedPaths=new Set(['/me','/workflow','/workday','/attendance','/onboarding','/dashboard','/shoot-control','/academy','/academy/coach?track=photographer','/academy/coach?track=booking']);
function cacheable(path){return cachedPaths.has(path)||/^\/bookings\?day=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/schedule\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/shoot-control\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/.test(path)||/^\/delivery\/\d+$/.test(path);}
export async function saveSnapshot(path,data){const id=identity();if(id&&cacheable(path))await localAction('snapshots','readwrite',s=>s.put({key:`${id}:${path}`,data,at:Date.now()}));}
export async function readSnapshot(path){const id=identity();if(!id||!cacheable(path))return null;const row=await localAction('snapshots','readonly',s=>s.get(`${id}:${path}`));if(!row||Date.now()-row.at>7*86400000)return null;return {...row.data,_offline:true,_cachedAt:row.at};}
