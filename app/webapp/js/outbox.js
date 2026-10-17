import {api} from './api.js';
import {localAction,openLocal} from './localstore.js';
let userId=null,running=false,timer=null,retryDue=0,rerunRequested=false,authEpoch=0;
const scheduleRetry=delay=>{
 const wait=Math.min(2147483647,Math.max(1000,delay)),due=Date.now()+wait;
 // Keep the earliest wake-up: a later blocked row must not postpone an earlier one.
 if(timer&&retryDue<=due)return;
 clearTimeout(timer);retryDue=due;
 timer=setTimeout(()=>{timer=null;retryDue=0;void flushOutbox();},wait);
};
const update=row=>localAction('operations','readwrite',s=>s.put(row));
const changed=()=>window.dispatchEvent(new Event('pb-outbox'));
// Older servers converted every domain error to this generic 400. Recheck such
// media once, with the original key and bytes; never reset arbitrary bad requests.
const LEGACY_MEDIA_RECOVERY='media-recovery-v2-20261017';
const legacyMediaError=row=>row.status==='error'&&row.errorStatus===400&&row.legacyMediaRecovery!==LEGACY_MEDIA_RECOVERY&&
 Boolean(row.hasBlob||row.blob)&&['shoot_photo','delivery_photo','sale_selected','booking_receipt','sale_receipt','attendance'].includes(row.kind)&&
 typeof row.error==='string'&&row.error.startsWith('Не удалось прочитать вложение.');
// Called only after /me has confirmed the active account. A previous 401 is not
// a permanent rejection of the saved file; retry it after this new login only.
const renewedAuth=row=>row.status==='error'&&row.errorStatus===401&&(row.authEpoch||0)<authEpoch;
export function setOutboxUser(id){userId=id;if(id)authEpoch=Math.max(Date.now(),authEpoch+1);return flushOutbox();}
export async function outboxRows(){
 if(!userId)return [];const actor=userId,db=await openLocal();
 try{return await new Promise((resolve,reject)=>{
  const rows=[],tx=db.transaction('operations','readonly'),request=tx.objectStore('operations').openCursor();
  request.onsuccess=()=>{const cursor=request.result;if(!cursor)return;const row=cursor.value;if(row.userId===actor){row.hasBlob=Boolean(row.blob);row.blob=null;rows.push(row);}cursor.continue();};
  tx.oncomplete=()=>resolve(rows.sort((a,b)=>a.createdAt-b.createdAt||(a.order||0)-(b.order||0)));
  tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('Не удалось прочитать очередь.'));
 });}finally{db.close();}
}
export async function enqueueBatch(items){
 if(!userId)throw new Error('Сначала откройте приложение при доступной сети.');
 if(!items.length)throw new Error('Нет действий для сохранения.');
 const at=Date.now(),actor=userId;
 const rows=items.map((item,i)=>({key:item.key||crypto.randomUUID(),userId:actor,kind:item.kind,date:item.date,data:item.data,blob:item.blob||null,filename:item.blob?.name||'',status:'local',createdAt:at,order:i}));
 await localAction('operations','readwrite',s=>{for(const row of rows)s.add(row);});
 changed();void flushOutbox();return rows;
}
export async function enqueueOperation(kind,date,data,blob=null){return (await enqueueBatch([{kind,date,data,blob}]))[0];}
// A definitive validation/size rejection is safe to correct. Never replace bytes
// after an uncertain timeout: that operation may already be committed server-side.
export async function replaceOperationFile(key,file){
 const row=await localAction('operations','readonly',s=>s.get(key));
 if(!row||row.userId!==userId||row.status!=='error'||![400,413].includes(row.errorStatus)||
    !['shoot_photo','delivery_photo','sale_selected','booking_receipt','sale_receipt','attendance'].includes(row.kind))throw new Error('Этот файл нельзя заменять: дождитесь результата отправки или повторите исходный файл.');
 const maximum=['booking_receipt','sale_receipt'].includes(row.kind)?8*1024*1024:20*1024*1024;
 if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type)||!file.size||file.size>maximum)throw new Error(`Нужен JPEG, PNG или WebP до ${maximum/1024/1024} МБ.`);
 row.blob=file;row.filename=file.name||row.filename;row.status='local';row.error='';row.errorStatus=0;row.retryAt=0;
 await update(row);changed();return flushOutbox();
}
// Store one original locally, send it, and wait for confirmation before loading
// the next file into IndexedDB. This bounds browser storage for large folders.
export async function enqueueFilesOneByOne(files,operationFor,onProgress=()=>{}){
 const actor=userId,total=files.length;
 if(!actor)throw new Error('Войдите в свой аккаунт Photo Boss.');
 if(!total)throw new Error('В выбранной папке нет подходящих фотографий.');
 for(let i=0;i<total;i++){
  if(actor!==userId)throw new Error('Аккаунт сменился. Выберите папку снова.');
  const file=files[i];
  // Wait for any previous upload to leave the shared worker before scheduling the next.
  while(running&&actor===userId)await new Promise(done=>setTimeout(done,100));
  if(actor!==userId)throw new Error('Аккаунт сменился. Выберите папку снова.');
  const [saved]=await enqueueBatch([{...operationFor(file,i),blob:file}]);
  onProgress({done:i,total,name:file.name,state:'sending'});
  for(;;){
   if(actor!==userId)throw new Error('Аккаунт сменился. Сохранённые фото остались в очереди прежнего аккаунта.');
   const current=await localAction('operations','readonly',s=>s.get(saved.key));
   if(!current)throw new Error('Кадр не найден в очереди.');
   if(current.status==='synced'){onProgress({done:i+1,total,name:file.name,state:'synced',result:current.result});break;}
   if(current.status==='error'||(current.status==='local'&&current.error)){
    onProgress({done:i,total,name:file.name,state:'paused'});
    throw new Error(`Остановка на «${file.name}»: ${current.error||'не удалось передать файл'}. Кадр остаётся в очереди, нажмите «Повторить».`);
   }
   if(!navigator.onLine)throw new Error('Нет сети. Текущий кадр сохранён на устройстве. Не очищайте браузер.');
   await new Promise(done=>setTimeout(done,350));
  }
 }
 return {done:total,total};
}

export async function retryOperation(key){const rows=await outboxRows();if(!rows.some(r=>r.key===key))return;const row=await localAction('operations','readonly',s=>s.get(key));if(!row||row.userId!==userId)return;row.status='local';row.error='';if(row.errorStatus!==429)row.retryAt=0;await update(row);for(const item of rows){if(item.errorStatus===424){const pending=await localAction('operations','readonly',s=>s.get(item.key));if(!pending||pending.userId!==userId)continue;pending.status='local';pending.error='';pending.retryAt=0;await update(pending);}}changed();return flushOutbox();}
export async function flushOutbox(){
 if(!userId)return {state:'no-account'};
 if(!navigator.onLine)return {state:'offline'};
 if(running){rerunRequested=true;return {state:'running'};}
 rerunRequested=false;
 running=true;
 const actor=userId,summary={state:'done',completed:0,failed:0,temporary:0};
 try{
  for(const item of await outboxRows()){
   if(actor!==userId)break;
   if(!['local','syncing'].includes(item.status)&&!legacyMediaError(item)&&!renewedAuth(item))continue;
   const row=await localAction('operations','readonly',s=>s.get(item.key));
   if(actor!==userId)break;
   if(!row||(!['local','syncing'].includes(row.status)&&!legacyMediaError(row)&&!renewedAuth(row)))continue;
   if(legacyMediaError(row)){row.legacyMediaRecovered=true;row.legacyMediaRecovery=LEGACY_MEDIA_RECOVERY;row.status='local';row.error='';await update(row);}
   if(renewedAuth(row)){row.status='local';row.error='';row.retryAt=0;}
   if(row.retryAt>Date.now()){scheduleRetry(row.retryAt-Date.now());summary.state='backoff';continue;}
   row.authEpoch=authEpoch;
   row.status='syncing';await update(row);changed();
   try{
    const operation={actorId:row.userId,key:row.key,kind:row.kind,date:row.date,data:row.data};
    let body=operation,path='/operations/sync';
    if(row.blob){
     const bytes=await row.blob.arrayBuffer();
     if(!bytes.byteLength){const error=new Error('Сохранённое фото пустое. Выберите файл заново.');error.status=400;throw error;}
     const upload=new Blob([bytes],{type:row.blob.type||'application/octet-stream'});
     body=new FormData();body.append('operation',JSON.stringify(operation));body.append('file',upload,row.filename||'upload');
     path='/operations/media';
    }
    row.result=await api(path,{method:'POST',body,timeoutMs:180000});
    row.status='synced';row.error='';row.errorStatus=0;row.retryAt=0;row.blob=null;
    summary.completed++;
   }catch(e){
    // Missing dependencies and receipt failures must not strand independent shoot photos.
    // Keep their blobs on this device and retry without marking a dependent sale as failed.
    const temporary=!e.status||e.status>=500||[408,424,429].includes(e.status);
    row.status=temporary?'local':'error';
    row.error=e.message;row.errorStatus=e.status||0;
    row.retryAt=temporary?Date.now()+Math.max(30000,e.status===429?e.retryAfterMs||0:0):0;
    await update(row);changed();
    if(temporary){
     summary.temporary++;
     scheduleRetry(Math.max(30000,row.retryAt-Date.now()));
     if(e.status===424||e.status>=500||['booking_receipt','sale_receipt'].includes(row.kind))continue;
     break;
    }
    summary.failed++;
    if(e.status===401)break;
    continue;
   }
   await update(row);changed();
  }
 }finally{running=false;changed();if(userId&&(userId!==actor||rerunRequested))void flushOutbox();}
 return summary;
}
window.addEventListener('online',()=>flushOutbox());
// Mobile browsers suspend timers in the background even while onLine stays true.
for(const event of ['focus','pageshow'])window.addEventListener(event,()=>flushOutbox());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void flushOutbox();});
