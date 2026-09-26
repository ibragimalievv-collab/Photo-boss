import {readSnapshot,saveSnapshot} from './localstore.js';
export class ApiError extends Error {constructor(message,status=0,telegramId=null){super(message);this.status=status;this.telegramId=Number.isSafeInteger(telegramId)&&telegramId>0?telegramId:null;}}

const SECURE_INIT_KEY='photo_boss_init_data_v1';
const SESSION_INIT_KEY='pb_init_data_v1';
const SESSION_OWNER_KEY='pb_owner_launch_v1';
const SESSION_MAX_AGE_SECONDS=23*60*60;

function sessionGet(key){try{return sessionStorage.getItem(key)||'';}catch{return '';}}
function sessionSet(key,value){try{if(value)sessionStorage.setItem(key,value);else sessionStorage.removeItem(key);}catch{}}
function ownerTokenFromHash(){
 try{
  const raw=location.hash.slice(1),query=raw.includes('?')?raw.slice(raw.indexOf('?')+1):'';
  return new URLSearchParams(query).get('owner_launch')||'';
 }catch{return '';}
}
function ownerTokenFresh(token){
 try{
  if(!token||!token.includes('.'))return false;
  const encoded=token.split('.',1)[0].replace(/-/g,'+').replace(/_/g,'/');
  const padded=encoded+'='.repeat((4-encoded.length%4)%4);
  const decoded=atob(padded),parts=decoded.split(':');
  const expires=Number(parts[parts.length-1]);
  return Number.isFinite(expires)&&expires>Math.floor(Date.now()/1000)+5;
 }catch{return false;}
}
const HASH_OWNER_LAUNCH_TOKEN=(()=>{
 const value=ownerTokenFromHash();
 if(ownerTokenFresh(value))sessionSet(SESSION_OWNER_KEY,value);
 return ownerTokenFresh(value)?value:'';
})();
function storedOwnerLaunchToken(){
 const value=sessionGet(SESSION_OWNER_KEY);
 if(ownerTokenFresh(value))return value;
 sessionSet(SESSION_OWNER_KEY,'');
 return '';
}
function initDataFresh(value){
 try{
  if(!value)return false;
  const issued=Number(new URLSearchParams(value).get('auth_date'));
  const now=Math.floor(Date.now()/1000);
  return Number.isFinite(issued)&&issued>0&&issued<=now+30&&issued>=now-SESSION_MAX_AGE_SECONDS;
 }catch{return false;}
}
function secureStorage(){return window.Telegram?.WebApp?.SecureStorage||null;}
function secureGet(key,timeoutMs=900){
 const storage=secureStorage();
 if(!storage?.getItem)return Promise.resolve('');
 return new Promise(resolve=>{
  let done=false;const finish=value=>{if(done)return;done=true;clearTimeout(timer);resolve(typeof value==='string'?value:'');};
  const timer=setTimeout(()=>finish(''),timeoutMs);
  try{storage.getItem(key,(error,value)=>finish(error?'':value));}catch{finish('');}
 });
}
function secureSet(key,value){
 const storage=secureStorage();
 if(!storage?.setItem||!value)return;
 try{storage.setItem(key,value,()=>{});}catch{}
}
function secureRemove(key){
 const storage=secureStorage();
 if(!storage?.removeItem)return;
 try{storage.removeItem(key,()=>{});}catch{}
}
function rememberInitData(value){
 if(!initDataFresh(value))return;
 sessionSet(SESSION_INIT_KEY,value);
 secureSet(SECURE_INIT_KEY,value);
}
function forgetInitData(){sessionSet(SESSION_INIT_KEY,'');secureRemove(SECURE_INIT_KEY);}

async function telegramInitData(waitMs=2500){
 const deadline=Date.now()+waitMs;
 while(Date.now()<deadline){
  const value=window.Telegram?.WebApp?.initData||'';
  if(value){rememberInitData(value);return {value,source:'live'};}
  await new Promise(resolve=>setTimeout(resolve,50));
 }
 const finalValue=window.Telegram?.WebApp?.initData||'';
 if(finalValue){rememberInitData(finalValue);return {value:finalValue,source:'live'};}
 const sessionValue=sessionGet(SESSION_INIT_KEY);
 if(initDataFresh(sessionValue))return {value:sessionValue,source:'session'};
 sessionSet(SESSION_INIT_KEY,'');
 const secureValue=await secureGet(SECURE_INIT_KEY);
 if(initDataFresh(secureValue)){sessionSet(SESSION_INIT_KEY,secureValue);return {value:secureValue,source:'secure'};}
 if(secureValue)secureRemove(SECURE_INIT_KEY);
 return {value:'',source:'none'};
}
function authHeaders(kind,value){
 if(kind==='owner')return {'X-PhotoBoss-Owner-Launch':value};
 if(kind==='telegram')return {'X-Telegram-Init-Data':value};
 return {};
}

export async function api(path,{body,method='GET',timeoutMs,responseType,...options}={}){
 const cfg=window.PHOTO_BOSS_CONFIG||{};
 const ownerToken=HASH_OWNER_LAUNCH_TOKEN||storedOwnerLaunchToken();
 const telegram=HASH_OWNER_LAUNCH_TOKEN?{value:'',source:'none'}:await telegramInitData();
 let primary=HASH_OWNER_LAUNCH_TOKEN?{kind:'owner',value:HASH_OWNER_LAUNCH_TOKEN,source:'hash'}:
             telegram.value?{kind:'telegram',value:telegram.value,source:telegram.source}:
             ownerToken?{kind:'owner',value:ownerToken,source:'session'}:null;
 if(!primary)throw new ApiError('Telegram не передал данные входа. Закройте окно Photo Boss и откройте приложение заново для обновления входа.',401);
 const alternate=primary.kind==='telegram'&&ownerToken?{kind:'owner',value:ownerToken,source:'session'}:
                 primary.kind==='owner'&&telegram.value?{kind:'telegram',value:telegram.value,source:telegram.source}:null;
 const base=String(cfg.API_BASE_URL||'').replace(/\/$/,'');
 const url=new URL(base+`/api/miniapp${path}`,location.origin);
 if(url.protocol!=='https:')throw new ApiError('Рабочее приложение должно открываться по HTTPS.');
 if(url.origin!==location.origin)throw new ApiError('API должен работать на том же адресе, что и приложение.');
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs||cfg.API_TIMEOUT_MS||20000);
 try {
  const multipart=body instanceof FormData;
  const payload=body===undefined?undefined:multipart?body:JSON.stringify(body);
  const send=credential=>fetch(url,{...options,method,signal:controller.signal,cache:'no-store',credentials:'same-origin',redirect:'error',headers:{...(!multipart?{'Content-Type':'application/json'}:{}),...authHeaders(credential.kind,credential.value)},body:payload});
  let response=await send(primary);
  if(response.status===401&&alternate){
   if(primary.kind==='telegram'&&primary.source!=='live')forgetInitData();
   response=await send(alternate);primary=alternate;
  }
  if(response.status===401&&primary.kind==='telegram'&&primary.source!=='live')forgetInitData();
  if(response.ok&&responseType==='blob')return await response.blob();
  const data=await response.json().catch(()=>null);
  if(!response.ok)throw new ApiError(data?.error||(response.status>=500?'Сервис временно недоступен. Повторите попытку.':`Запрос отклонён (${response.status}).`),response.status,data?.telegramId);
  if(!data)throw new ApiError('Сервер вернул некорректный ответ.');
  if(method==='GET')try{await saveSnapshot(path,data);}catch{window.dispatchEvent(new Event('pb-local-storage-error'));}
  return data;
 }catch(e){if(method==='GET'&&!e.status)try{const cached=await readSnapshot(path);if(cached)return cached;}catch{}if(e.name==='AbortError')throw new ApiError('Сервер отвечает дольше обычного. Повторите попытку.');if(e instanceof ApiError)throw e;throw new ApiError('Нет соединения с сервером. Демо-данные не подставляются.');}finally{clearTimeout(timer);}
}
