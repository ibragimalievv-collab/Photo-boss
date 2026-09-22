import {readSnapshot,saveSnapshot} from './localstore.js';
export class ApiError extends Error {constructor(message,status=0,telegramId=null){super(message);this.status=status;this.telegramId=Number.isSafeInteger(telegramId)&&telegramId>0?telegramId:null;}}
const OWNER_LAUNCH_TOKEN=(()=>{try{const raw=location.hash.slice(1);const query=raw.includes('?')?raw.slice(raw.indexOf('?')+1):'';return new URLSearchParams(query).get('owner_launch')||'';}catch{return '';}})();
async function telegramInitData(waitMs=2500){
 const deadline=Date.now()+waitMs;
 while(Date.now()<deadline){
  const value=window.Telegram?.WebApp?.initData;
  if(value)return value;
  await new Promise(resolve=>setTimeout(resolve,50));
 }
 return window.Telegram?.WebApp?.initData||'';
}
export async function api(path,{body,method='GET',timeoutMs,responseType,...options}={}){
 const cfg=window.PHOTO_BOSS_CONFIG||{};
 const initData=await telegramInitData();
 if(!initData&&!OWNER_LAUNCH_TOKEN)throw new ApiError('Telegram не передал данные входа. Закройте окно Photo Boss и откройте приложение заново для обновления входа.',401);
 const base=String(cfg.API_BASE_URL||'').replace(/\/$/,'');
 const url=new URL(base+`/api/miniapp${path}`,location.origin);
 if(url.protocol!=='https:')throw new ApiError('Рабочее приложение должно открываться по HTTPS.');
 if(url.origin!==location.origin)throw new ApiError('API должен работать на том же адресе, что и приложение.');
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs||cfg.API_TIMEOUT_MS||20000);
 try {
  const multipart=body instanceof FormData;
  const response=await fetch(url,{...options,method,signal:controller.signal,cache:'no-store',credentials:'omit',redirect:'error',headers:{...(!multipart?{'Content-Type':'application/json'}:{}),...(initData?{'X-Telegram-Init-Data':initData}:{'X-PhotoBoss-Owner-Launch':OWNER_LAUNCH_TOKEN})},body:body===undefined?undefined:multipart?body:JSON.stringify(body)});
  if(response.ok&&responseType==='blob')return await response.blob();
  const data=await response.json().catch(()=>null);
  if(!response.ok)throw new ApiError(data?.error||(response.status>=500?'Сервис временно недоступен. Повторите попытку.':`Запрос отклонён (${response.status}).`),response.status,data?.telegramId);
  if(!data)throw new ApiError('Сервер вернул некорректный ответ.');
  if(method==='GET')try{await saveSnapshot(path,data);}catch{window.dispatchEvent(new Event('pb-local-storage-error'));}
  return data;
 }catch(e){if(method==='GET'&&!e.status)try{const cached=await readSnapshot(path);if(cached)return cached;}catch{}if(e.name==='AbortError')throw new ApiError('Сервер отвечает дольше обычного. Повторите попытку.');if(e instanceof ApiError)throw e;throw new ApiError('Нет соединения с сервером. Демо-данные не подставляются.');}finally{clearTimeout(timer);}
}
