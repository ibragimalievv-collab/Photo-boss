import {readSnapshot,saveSnapshot} from './localstore.js';
export class ApiError extends Error {constructor(message,status=0){super(message);this.status=status;}}
export async function api(path,{body,method='GET',timeoutMs,...options}={}){
 const cfg=window.PHOTO_BOSS_CONFIG||{};
 const initData=window.Telegram?.WebApp?.initData;
 if(!initData)throw new ApiError('Откройте Photo Boss кнопкой приложения внутри Telegram.',401);
 const base=String(cfg.API_BASE_URL||'').replace(/\/$/,'');
 const url=new URL(base+`/api/miniapp${path}`,location.origin);
 if(url.protocol!=='https:')throw new ApiError('Рабочее приложение должно открываться по HTTPS.');
 if(url.origin!==location.origin)throw new ApiError('API должен работать на том же адресе, что и приложение.');
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs||cfg.API_TIMEOUT_MS||20000);
 try {
  const multipart=body instanceof FormData;
  const response=await fetch(url,{...options,method,signal:controller.signal,cache:'no-store',credentials:'omit',redirect:'error',headers:{...(!multipart?{'Content-Type':'application/json'}:{}),'X-Telegram-Init-Data':initData},body:body===undefined?undefined:multipart?body:JSON.stringify(body)});
  const data=await response.json().catch(()=>null);
  if(!response.ok)throw new ApiError(data?.error||(response.status>=500?'Сервис временно недоступен. Повторите попытку.':`Запрос отклонён (${response.status}).`),response.status);
  if(!data)throw new ApiError('Сервер вернул некорректный ответ.');
  if(method==='GET')try{await saveSnapshot(path,data);}catch{window.dispatchEvent(new Event('pb-local-storage-error'));}
  return data;
 }catch(e){if(method==='GET'&&!e.status)try{const cached=await readSnapshot(path);if(cached)return cached;}catch{}if(e.name==='AbortError')throw new ApiError('Сервер отвечает дольше обычного. Повторите попытку.');if(e instanceof ApiError)throw e;throw new ApiError('Нет соединения с сервером. Демо-данные не подставляются.');}finally{clearTimeout(timer);}
}
