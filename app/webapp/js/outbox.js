import {api} from './api.js';
import {localAction,openLocal,writeOperations} from './localstore.js';
let userId=null,operationsGeneration='',salesGeneration='',running=false,timer=null,retryDue=0,rerunRequested=false,authEpoch=0,queueRepairing=false;
const scheduleRetry=delay=>{
 const wait=Math.min(2147483647,Math.max(1000,delay)),due=Date.now()+wait;
 // Keep the earliest wake-up: a later blocked row must not postpone an earlier one.
 if(timer&&retryDue<=due)return;
 clearTimeout(timer);retryDue=due;
 timer=setTimeout(()=>{timer=null;retryDue=0;void flushOutbox();},wait);
};
const update=row=>writeOperations([row]);
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
// A sale's receipt, counts, selected photos and completion share one draft key.
// Their server-side stages must stay ordered while unrelated files keep moving.
const saleChain=row=>row.kind==='sale_start'?row.key:row.kind?.startsWith('sale_')?(row.data?.draft?.key||(row.data?.draft?.id?'draft:'+row.data.draft.id:null)):null;
const countCompletionError=row=>row?.kind==='sale_complete'&&row.status==='error'&&row.errorStatus===409&&(
 /^Сначала загрузите все выбранные фотографии: \d+\/\d+\.$/.test(row.error||'')||
 /^Загружено \d+ выбранных кадров, а продано указано \d+\. Исправьте количество кадров в этой продаже\.$/.test(row.error||'')||
 /^Полная съёмка содержит \d+ кадров\. Общее количество менять нельзя\.$/.test(row.error||'')||
 row.error==='Количество проданных кадров изменилось. Проверьте продажу.');
export function rejectedSaleCounts(rows){
 // A completed original upload can invalidate the total from an older form.
 // This exact rejection rolls the request back: none of its later photos may
 // have started before replacing that rejected total under the original key.
 const index=rows.findIndex(row=>row?.kind==='sale_counts'&&row.status==='error'&&row.errorStatus===409&&
  /^Полная съёмка содержит \d+ кадров\. Общее количество менять нельзя\.$/.test(row.error||''));
 if(index<0||!rows.every(Boolean)||!rows.slice(0,index).every(row=>row.status==='synced')||
  !rows.slice(index+1).every(row=>row.status==='local'&&!row.errorStatus&&!row.result))return null;
 return rows[index];
}
export function saleQueueRepairable(rows){
 const completions=rows.filter(row=>row?.kind==='sale_complete'),complete=completions[0];
 const rejected=rejectedSaleCounts(rows);
 if(rejected)return Boolean(completions.length===1&&rows.some(row=>row.kind==='sale_start'&&row.result?.draftId)&&
  rows.every(row=>saleChain(row)===saleChain(rejected)));
 return Boolean(rows.length>1&&rows.every(Boolean)&&completions.length===1&&countCompletionError(complete)&&
  rows.some(row=>row.kind==='sale_start'&&row.result?.draftId)&&rows.every(row=>row===complete||row.status==='synced')&&
  (!complete.resumeByKey||rows.some(row=>row.key===complete.resumeByKey&&row.status==='synced')));
}
export async function repairSaleQueue(keys,{draftId,total,sold,price},files=[]){
 if(running||queueRepairing)throw new Error('Дождитесь завершения текущей отправки, затем исправьте продажу.');
 queueRepairing=true;let committed=false;
 try{
 const actor=userId,all=await outboxRows(),rows=keys.map(key=>all.find(row=>row.key===key));
 const complete=rows.find(row=>row?.kind==='sale_complete'),start=rows.find(row=>row?.kind==='sale_start');
 if(!actor||!saleQueueRepairable(rows)||start.result.draftId!==draftId||!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold>total||total>10000)
  throw new Error('Исправление недоступно: сначала дождитесь результата всех фото и чека.');
 const rejected=rejectedSaleCounts(rows);
 if(rejected){
  if(files.length||sold!==rejected.data.sold)throw new Error('Исправьте только общее количество кадров. Выбранные фото и проданное количество уже сохранены в очереди.');
  rejected.data={...rejected.data,total,price};rejected.status='local';rejected.error='';rejected.errorStatus=0;rejected.retryAt=0;
  if(actor!==userId||running)throw new Error('Состояние отправки изменилось. Откройте исправление продажи ещё раз.');
  if(!await update(rejected))throw new Error('Рабочие данные обнулены. Обновите приложение перед продолжением.');
  committed=true;changed();return [];
 }
 if(complete.resumeByKey&&!all.some(row=>row.key===complete.resumeByKey&&row.status==='synced'))throw new Error('Предыдущее исправление ещё отправляется.');
 for(const file of files)if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type)||!file.size||file.size>20*1024*1024)throw new Error('Нужен JPEG, PNG или WebP до 20 МБ.');
 const at=Date.now(),draft={id:draftId},items=[{kind:'sale_counts',data:{draft,total,sold,price}},...files.map(blob=>({kind:'sale_selected',data:{draft},blob}))];
 const added=items.map((item,order)=>({...item,key:crypto.randomUUID(),userId:actor,generation:operationsGeneration,salesGeneration,date:complete.date,blob:item.blob||null,filename:item.blob?.name||'',status:'local',createdAt:at,order}));
 const last=added[added.length-1];last.resumeCompleteKey=complete.key;
 // Only local recovery metadata changes. The original completion key and its
 // request payload remain untouched; accepted receipt/photo operations stay put.
 complete.resumeByKey=last.key;
 if(actor!==userId||running)throw new Error('Состояние отправки изменилось. Откройте исправление продажи ещё раз.');
 if(!await writeOperations([...added,complete],{addKeys:added.map(row=>row.key)}))throw new Error('Рабочие данные обнулены. Обновите приложение перед продолжением.');
 committed=true;changed();return added;
 }finally{queueRepairing=false;if(committed||rerunRequested)void flushOutbox();}
}
async function resumeRepairedCompletion(repair){
 if(repair.userId!==userId||repair.status!=='synced'||!repair.resumeCompleteKey)return;
 const complete=await localAction('operations','readonly',store=>store.get(repair.resumeCompleteKey));
 if(complete?.userId!==userId||complete.resumeByKey!==repair.key||complete.repairAppliedKey===repair.key||!countCompletionError(complete))return;
 complete.status='local';complete.error='';complete.errorStatus=0;complete.retryAt=0;complete.repairAppliedKey=repair.key;
 await update(complete);rerunRequested=true;changed();
}
const SALE_DEPENDENCY_RECOVERY='sale-dependency-recovery-v1-20261017';
function legacySaleDependency(row,previous){
 if(row.resumeByKey||row.status!=='error'||row.errorStatus!==409||row.saleDependencyRecovery===SALE_DEPENDENCY_RECOVERY)return false;
 const required={sale_counts:'sale_receipt',sale_selected:'sale_counts',sale_complete:'sale_selected'}[row.kind];
 const stageError=row.kind==='sale_counts'?row.error==='Черновик продажи уже закрыт.':row.kind==='sale_selected'?row.error==='Сначала сохраните чек и количество кадров.':row.kind==='sale_complete'&&(row.error==='Продажа уже завершена или отменена.'||/^Сначала загрузите все выбранные фотографии: \d+\/\d+\.$/.test(row.error||''));
 // Old clients sent later sale stages after a receipt/photo failed. Recheck
 // only those exact failures once their original local predecessors succeeded.
 return Boolean(required&&stageError&&previous.some(step=>step.kind==='sale_start')&&previous.some(step=>step.kind===required)&&previous.every(step=>step.status==='synced'));
}
export function setOutboxUser(id,generation='',saleGeneration=''){userId=id;salesGeneration=typeof saleGeneration==='string'?saleGeneration:'';operationsGeneration=typeof generation==='string'?generation:'';if(id)authEpoch=Math.max(Date.now(),authEpoch+1);return flushOutbox();}
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
 const rows=items.map((item,i)=>({key:item.key||crypto.randomUUID(),userId:actor,generation:operationsGeneration,salesGeneration,kind:item.kind,date:item.date,data:item.data,blob:item.blob||null,filename:item.blob?.name||'',status:'local',createdAt:at,order:i}));
 if(!await writeOperations(rows,{add:true}))throw new Error('Рабочие данные обнулены. Обновите приложение перед продолжением.');
 changed();void flushOutbox();return rows;
}
export async function enqueueOperation(kind,date,data,blob=null){return (await enqueueBatch([{kind,date,data,blob}]))[0];}
// A definitive validation/size rejection is safe to correct. Never replace bytes
// after an uncertain timeout: that operation may already be committed server-side.
const duplicateSelectedRejection=row=>row.kind==='sale_selected'&&row.errorStatus===409&&row.error==='Этот кадр уже куплен в предыдущей продаже. Выберите новый кадр для допродажи.';
const duplicateReceiptRejection=row=>row.kind==='sale_receipt'&&row.errorStatus===409&&row.error==='Этот чек уже использовался или похож на ранее загруженный.';
export function canReplaceOperationFile(row){
 return Boolean(row?.status==='error'&&([400,413].includes(row.errorStatus)||duplicateSelectedRejection(row)||duplicateReceiptRejection(row))&&
  ['shoot_photo','delivery_photo','sale_selected','booking_receipt','sale_receipt'].includes(row.kind));
}
export async function replaceOperationFile(key,file){
 const row=await localAction('operations','readonly',s=>s.get(key));
 if(!row||row.userId!==userId||!canReplaceOperationFile(row))throw new Error('Этот файл нельзя заменять: дождитесь результата отправки или повторите исходный файл.');
 const maximum=['booking_receipt','sale_receipt'].includes(row.kind)?8*1024*1024:20*1024*1024;
 if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type)||!file.size||file.size>maximum)throw new Error(`Нужен JPEG, PNG или WebP до ${maximum/1024/1024} МБ.`);
 row.blob=file;row.filename=file.name||row.filename;row.status='local';row.error='';row.errorStatus=0;row.retryAt=0;
 await update(row);changed();return flushOutbox();
}
// Store one original locally, send it, and wait for confirmation before loading
// the next file into IndexedDB. This bounds browser storage for large folders.
export async function enqueueFilesOneByOne(files,operationFor,onProgress=()=>{}){
 const actor=userId,generation=operationsGeneration,total=files.length;
 if(!actor)throw new Error('Войдите в свой аккаунт Photo Boss.');
 if(!total)throw new Error('В выбранной папке нет подходящих фотографий.');
 for(let i=0;i<total;i++){
  if(actor!==userId||generation!==operationsGeneration)throw new Error('Рабочие данные или аккаунт изменились. Выберите папку снова.');
  const file=files[i];
  // Wait for any previous upload to leave the shared worker before scheduling the next.
  while(running&&actor===userId)await new Promise(done=>setTimeout(done,100));
  if(actor!==userId||generation!==operationsGeneration)throw new Error('Рабочие данные или аккаунт изменились. Выберите папку снова.');
  const [saved]=await enqueueBatch([{...operationFor(file,i),blob:file}]);
  onProgress({done:i,total,name:file.name,state:'sending'});
  for(;;){
   if(actor!==userId||generation!==operationsGeneration)throw new Error('Рабочие данные или аккаунт изменились. Обновите раздел перед продолжением.');
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
// Explicit user retry may bypass our local network-failure cooldown, never a
// server Retry-After or a permanent access/validation error. Keep original keys
// and bytes so a lost successful response is recovered idempotently.
export async function syncOutboxNow(){
 const actor=userId;
 for(const item of await outboxRows()){
  if(actor!==userId)break;
  if(item.status!=='local'||item.errorStatus===429||!item.retryAt)continue;
  const row=await localAction('operations','readonly',s=>s.get(item.key));
  if(actor!==userId)break;
  if(!row||row.userId!==actor||row.status!=='local'||row.errorStatus===429)continue;
  row.retryAt=0;await update(row);
 }
 changed();return flushOutbox();
}
export async function flushOutbox(){
 if(!userId)return {state:'no-account'};
 if(!navigator.onLine)return {state:'offline'};
 if(running||queueRepairing){rerunRequested=true;return {state:'running'};}
 rerunRequested=false;
 running=true;
 const actor=userId,generation=operationsGeneration,active=()=>actor===userId&&generation===operationsGeneration,summary={state:'done',completed:0,failed:0,temporary:0},blockedSales=new Set(),saleSteps=new Map();
 try{
  // A tab may close after the correction was accepted but before its original
  // completion was reactivated. Recover that link on the next foreground pass.
  for(const repair of await outboxRows())if(repair.status==='synced'&&repair.resumeCompleteKey)await resumeRepairedCompletion(repair);
  for(const item of await outboxRows()){
   if(!active())break;
   // Legacy rows remain legacy packets; never stamp a freshly adopted reset
   // generation onto an old action, even if another tab held a stale cursor.
   if(item.kind!=='cash_expense'&&(item.generation||'')!==generation)continue;
   if(item.resumeByKey){const repair=await localAction('operations','readonly',store=>store.get(item.resumeByKey));if(repair?.status!=='synced')continue;}
   const sale=saleChain(item);
   if(sale&&blockedSales.has(sale))continue;
   const previous=sale?saleSteps.get(sale)||[]:[];
   if(sale&&!saleSteps.has(sale))saleSteps.set(sale,previous);
   if(!['local','syncing'].includes(item.status)&&!legacyMediaError(item)&&!renewedAuth(item)&&!legacySaleDependency(item,previous)){if(sale){previous.push(item);if(item.status!=='synced')blockedSales.add(sale);}continue;}
   const row=await localAction('operations','readonly',s=>s.get(item.key));
   if(!active())break;
   if(!row)continue;
   if(!['local','syncing'].includes(row.status)&&!legacyMediaError(row)&&!renewedAuth(row)&&!legacySaleDependency(row,previous)){if(sale){previous.push(row);if(row.status!=='synced')blockedSales.add(sale);}continue;}
   if(legacySaleDependency(row,previous)){row.saleDependencyRecovery=SALE_DEPENDENCY_RECOVERY;row.status='local';row.error='';row.retryAt=0;await update(row);}
   if(sale)previous.push(row);
   if(legacyMediaError(row)){row.legacyMediaRecovered=true;row.legacyMediaRecovery=LEGACY_MEDIA_RECOVERY;row.status='local';row.error='';await update(row);}
   if(renewedAuth(row)){row.status='local';row.error='';row.retryAt=0;}
   if(row.retryAt>Date.now()){if(sale)blockedSales.add(sale);scheduleRetry(row.retryAt-Date.now());summary.state='backoff';continue;}
   row.authEpoch=authEpoch;
   row.status='syncing';if(!await update(row))break;changed();
   try{
    if(!active())break;
    const operation={actorId:row.userId,key:row.key,kind:row.kind,date:row.date,data:row.data,...(row.generation!==undefined?{generation:row.generation}:{}),...(row.kind.startsWith('sale_')&&row.salesGeneration?{salesGeneration:row.salesGeneration}:{})};
    let body=operation,path='/operations/sync';
    if(row.blob){
     const bytes=await row.blob.arrayBuffer();
     if(!bytes.byteLength){const error=new Error('Сохранённое фото пустое. Выберите файл заново.');error.status=400;throw error;}
     const upload=new Blob([bytes],{type:row.blob.type||'application/octet-stream'});
     body=new FormData();body.append('operation',JSON.stringify(operation));body.append('file',upload,row.filename||'upload');
     path='/operations/media';
    }
    row.result=await api(path,{method:'POST',body,timeoutMs:180000});
    if(!active())break;
    row.status='synced';row.error='';row.errorStatus=0;row.retryAt=0;row.blob=null;
    summary.completed++;
   }catch(e){
    if(!active())break;
    if(sale)blockedSales.add(sale);
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
   await update(row);changed();if(row.resumeCompleteKey)await resumeRepairedCompletion(row);
  }
 }finally{running=false;changed();if(userId&&(!active()||rerunRequested))void flushOutbox();}
 return summary;
}
window.addEventListener('online',()=>flushOutbox());
// Mobile browsers suspend timers in the background even while onLine stays true.
for(const event of ['focus','pageshow'])window.addEventListener(event,()=>flushOutbox());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void flushOutbox();});
window.addEventListener('pb-operations-reset',event=>{if(event.detail?.userId===userId){operationsGeneration=event.detail.generation;clearTimeout(timer);timer=null;retryDue=0;rerunRequested=false;}});
