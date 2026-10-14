import {api} from './api.js';
import {localAction,openLocal} from './localstore.js';
let userId=null,running=false,timer=null,rerun=false;
const update=row=>localAction('operations','readwrite',s=>s.put(row));
const changed=()=>window.dispatchEvent(new Event('pb-outbox'));
export function setOutboxUser(id){userId=id;void flushOutbox();}
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
// Store one original locally, send it, and wait for confirmation before loading
// the next file into IndexedDB. This bounds browser storage for large folders.
export async function enqueueFilesOneByOne(files,operationFor,onProgress=()=>{}){
 const actor=userId,total=files.length;
 if(!actor)throw new Error('Войдите в свой аккаунт Photo Boss.');
 if(!total)throw new Error('В выбранной папке нет подходящих фотографий.');
 for(let i=0;i<total;i++){
  if(actor!==userId)throw new Error('Аккаунт сменился. Выберите папку снова.');
  const file=files[i];
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

export async function retryOperation(key){const rows=await outboxRows();if(!rows.some(r=>r.key===key))return;const row=await localAction('operations','readonly',s=>s.get(key));if(!row||row.userId!==userId)return;row.status='local';row.error='';await update(row);for(const item of rows){if(item.errorStatus===424){const pending=await localAction('operations','readonly',s=>s.get(item.key));if(!pending||pending.userId!==userId)continue;pending.status='local';pending.error='';await update(pending);}}changed();return flushOutbox();}
export async function flushOutbox(){
 if(!userId)return {state:'no-account'};
 if(!navigator.onLine)return {state:'offline'};
 if(running){rerun=true;return {state:'running'};}
 running=true;
 const actor=userId,summary={state:'done',completed:0,failed:0,temporary:0};
 try{
  for(const item of await outboxRows()){
   if(actor!==userId)break;
   if(!['local','syncing'].includes(item.status))continue;
   const row=await localAction('operations','readonly',s=>s.get(item.key));
   if(actor!==userId)break;
   if(!row||!['local','syncing'].includes(row.status))continue;
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
    row.status='synced';row.error='';row.errorStatus=0;row.blob=null;
    summary.completed++;
   }catch(e){
    // Missing dependencies and receipt failures must not strand independent shoot photos.
    // Keep their blobs on this device and retry without marking a dependent sale as failed.
    const temporary=!e.status||e.status>=500||e.status===424;
    row.status=temporary?'local':'error';
    row.error=e.message;row.errorStatus=e.status||0;
    await update(row);changed();
    if(temporary){
     summary.temporary++;
     clearTimeout(timer);timer=setTimeout(()=>flushOutbox(),30000);
     if(e.status===424||(e.status>=500&&['booking_receipt','sale_receipt'].includes(row.kind)))continue;
     break;
    }
    summary.failed++;
    if([401,403].includes(e.status))break;
    continue;
   }
   await update(row);changed();
  }
 }finally{running=false;changed();if(rerun){rerun=false;queueMicrotask(()=>void flushOutbox());}}
 return summary;
}
window.addEventListener('online',()=>flushOutbox());

