import {api} from './api.js';
import {localAction,openLocal,writeOperations} from './localstore.js';
let userId=null,operationsGeneration='',salesGeneration='',running=false,timer=null,retryDue=0,rerunRequested=false,authEpoch=0,queueRepairing=false;
// A camera photo or a large sale original must not delay a new booking. Keep
// bounded, independent workers, while preserving the original order of every
// sale chain and of attendance events. All workers retain the same durable keys.
const workers=new Set(),requestedWorkers=new Set();
const shiftKinds=new Set(['attendance','break_start','break_end','checklist','shift_report']);
const operationWorker=row=>row.kind==='booking'?'booking':shiftKinds.has(row.kind)?'attendance':isBankGate(row)?'bank':'media';
const isBankGate=row=>['booking_bank_payment','sale_bank_payment'].includes(row.kind);
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
const BOOKING_VALIDATION_ERRORS=new Set([
 'Проверьте данные гостя, дату, количество гостей и сумму брони.',
 'Отель или пакет больше не доступны. Обновите данные.',
 'Нет доступа к записи в этом отеле.',
 'В отеле не настроена фиксированная цена кадра. Попросите владельца или администратора выбрать цену в тарифах отеля.',
]);
export function rejectedBooking(rows){
 const row=rows[0];
 // These domain validations roll back before the idempotency ledger is written.
 // Never alter an acknowledged, uncertain, auth/reset or unknown rejection.
 return Boolean(rows.length&&rows.every(Boolean)&&row.kind==='booking'&&row.status==='error'&&
  row.errorStatus===409&&!row.result&&BOOKING_VALIDATION_ERRORS.has(row.error)&&
  rows.slice(1).every(item=>item.kind==='booking_receipt'&&item.data?.booking?.key===row.key&&
   item.status==='local'&&!item.result&&(!item.errorStatus||item.errorStatus===424)))?row:null;
}
export async function repairBookingQueue(keys,data){
 if(running||queueRepairing)throw new Error('Дождитесь завершения текущей отправки, затем исправьте запись.');
 queueRepairing=true;let committed=false;
 try{
  const actor=userId,all=await outboxRows(),rows=keys.map(key=>all.find(row=>row.key===key)),rejected=rejectedBooking(rows);
  if(!actor||!rejected||(rejected.generation||'')!==operationsGeneration||
   (rejected.salesGeneration||'')!==salesGeneration)throw new Error('Исправление записи недоступно. Обновите приложение и проверьте результат прежней отправки.');
  const saved=await localAction('operations','readonly',store=>store.get(rejected.key));
  if(!saved||saved.userId!==actor||!rejectedBooking([saved,...rows.slice(1)]))throw new Error('Состояние записи изменилось. Откройте исправление ещё раз.');
  saved.data=data;saved.status='local';saved.error='';saved.errorStatus=0;saved.retryAt=0;
  const updates=[saved];
  for(const item of rows.slice(1)){
   const receipt=await localAction('operations','readonly',store=>store.get(item.key));
   if(!receipt||receipt.userId!==actor||receipt.status!=='local'||receipt.result||
    (receipt.errorStatus&&receipt.errorStatus!==424))throw new Error('Состояние чека изменилось. Откройте исправление ещё раз.');
   receipt.error='';receipt.errorStatus=0;receipt.retryAt=0;updates.push(receipt);
  }
  if(actor!==userId||running)throw new Error('Аккаунт или состояние отправки изменились. Откройте исправление ещё раз.');
  if(!await writeOperations(updates))throw new Error('Рабочие данные обнулены. Обновите приложение перед продолжением.');
  committed=true;changed();
 }finally{queueRepairing=false;if(committed||rerunRequested)void flushOutbox();}
}
const INLINE_VALIDATION_ERRORS=new Set([
 'Проверьте данные новой продажи.','Нет доступа к продаже в этом отеле.',
 'Отель больше не доступен. Обновите данные.','Выберите действующего фотографа этого отеля.',
 'Укажите имя гостя, корректные дату и время съёмки.','Пакет больше не доступен. Обновите данные.',
 'Выберите пакет или настройте фиксированную цену кадра в отеле.','Отель недоступен.',
 'Цена пакета изменилась. Обновите данные и проверьте продажу.','Тариф недоступен в этом отеле.',
 'В отеле не настроена фиксированная цена кадра. Попросите владельца или администратора выбрать цену в тарифах отеля.',
]);
export function rejectedInlineSale(rows){
 const start=rows[0];
 return Boolean(rows.length>1&&rows.every(Boolean)&&start.kind==='sale_start_inline'&&start.status==='error'&&
  start.errorStatus===409&&!start.result&&INLINE_VALIDATION_ERRORS.has(start.error)&&
  rows.slice(1).every(row=>row.kind.startsWith('sale_')&&row.data?.draft?.key===start.key&&
   row.status==='local'&&!row.result&&(!row.errorStatus||row.errorStatus===424)))?start:null;
}
export async function repairInlineSaleQueue(keys,startData){
 if(running||queueRepairing)throw new Error('Дождитесь завершения текущей отправки, затем исправьте продажу.');
 queueRepairing=true;let committed=false;
 try{
  const actor=userId,all=await outboxRows(),rows=keys.map(key=>all.find(row=>row.key===key)),rejected=rejectedInlineSale(rows);
  if(!actor||!rejected||(rejected.generation||'')!==operationsGeneration||(rejected.salesGeneration||'')!==salesGeneration)
   throw new Error('Исправление недоступно. Проверьте результат прежней отправки.');
  const saved=[];
  for(const row of rows){const fresh=await localAction('operations','readonly',store=>store.get(row.key));if(!fresh||fresh.userId!==actor)throw new Error('Состояние отправки изменилось.');saved.push(fresh);}
  if(!rejectedInlineSale(saved))throw new Error('Состояние продажи изменилось. Обновите данные.');
  saved[0].data=startData;
  for(const [index,row] of saved.entries()){
   row.status='local';row.error='';row.errorStatus=0;row.retryAt=0;row.saleTransientFailures=0;row.autoRetryPaused=false;
   if(index&&['sale_counts','sale_complete'].includes(row.kind))row.data={...row.data,price:startData.price,...(row.kind==='sale_counts'?{tariffId:startData.tariffId||null}: {})};
   if(row.kind==='sale_bank_payment')row.data={...row.data,payment:{...row.data.payment,tariffId:startData.tariffId||null}};
  }
  if(actor!==userId||running)throw new Error('Аккаунт или отправка изменились.');
  if(!await writeOperations(saved))throw new Error('Рабочие данные обнулены. Обновите приложение.');
  committed=true;changed();
 }finally{queueRepairing=false;if(committed||rerunRequested)void flushOutbox();}
}
// A sale's receipt, counts, selected photos and completion share one draft key.
// Their server-side stages must stay ordered while unrelated files keep moving.
const isSaleStart=row=>['sale_start','sale_start_inline'].includes(row?.kind);
const saleChain=row=>isSaleStart(row)?row.key:row.kind?.startsWith('sale_')?(row.data?.draft?.key||(row.data?.draft?.id?'draft:'+row.data.draft.id:null)):null;
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
 if(rejected)return Boolean(completions.length===1&&rows.some(row=>isSaleStart(row)&&row.result?.draftId)&&
  rows.every(row=>saleChain(row)===saleChain(rejected)));
 return Boolean(rows.length>1&&rows.every(Boolean)&&completions.length===1&&countCompletionError(complete)&&
  rows.some(row=>isSaleStart(row)&&row.result?.draftId)&&rows.every(row=>row===complete||row.status==='synced')&&
  (!complete.resumeByKey||rows.some(row=>row.key===complete.resumeByKey&&row.status==='synced')));
}
export async function repairSaleQueue(keys,{draftId,total,sold,price},files=[],diskPhotos=[]){
 if(running||queueRepairing)throw new Error('Дождитесь завершения текущей отправки, затем исправьте продажу.');
 queueRepairing=true;let committed=false;
 try{
 const actor=userId,all=await outboxRows(),rows=keys.map(key=>all.find(row=>row.key===key));
 const complete=rows.find(row=>row?.kind==='sale_complete'),start=rows.find(row=>isSaleStart(row));
 if(!actor||!saleQueueRepairable(rows)||start.result.draftId!==draftId||!Number.isInteger(total)||!Number.isInteger(sold)||sold<1||sold>total||total>10000)
  throw new Error('Исправление недоступно: сначала дождитесь результата всех фото и чека.');
 const rejected=rejectedSaleCounts(rows);
 if(rejected){
  if(files.length||diskPhotos.length||sold!==rejected.data.sold)throw new Error('Исправьте только общее количество кадров. Выбранные фото и проданное количество уже сохранены в очереди.');
  rejected.data={...rejected.data,total,price};rejected.status='local';rejected.error='';rejected.errorStatus=0;rejected.retryAt=0;
  if(actor!==userId||running)throw new Error('Состояние отправки изменилось. Откройте исправление продажи ещё раз.');
  if(!await update(rejected))throw new Error('Рабочие данные обнулены. Обновите приложение перед продолжением.');
  committed=true;changed();return [];
 }
 if(complete.resumeByKey&&!all.some(row=>row.key===complete.resumeByKey&&row.status==='synced'))throw new Error('Предыдущее исправление ещё отправляется.');
 for(const file of files)if(!file||!['image/jpeg','image/png','image/webp'].includes(file.type)||!file.size||file.size>20*1024*1024)throw new Error('Нужен JPEG, PNG или WebP до 20 МБ.');
 for(const photo of diskPhotos)if(!photo||typeof photo.path!=='string'||typeof photo.revision!=='string')throw new Error('Выберите фотографии из привязанной папки.');
 const at=Date.now(),draft={id:draftId},items=[{kind:'sale_counts',data:{draft,total,sold,price}},...files.map(blob=>({kind:'sale_selected',data:{draft},blob})),...diskPhotos.map(photo=>({kind:'sale_disk_selected',data:{draft,...photo}}))];
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
 await update(complete);requestedWorkers.add('media');changed();
}
const SALE_DEPENDENCY_RECOVERY='sale-dependency-recovery-v1-20261017';
function legacySaleDependency(row,previous){
 if(row.resumeByKey||row.status!=='error'||row.errorStatus!==409||row.saleDependencyRecovery===SALE_DEPENDENCY_RECOVERY)return false;
 const required={sale_counts:'sale_receipt',sale_selected:'sale_counts',sale_complete:'sale_selected'}[row.kind];
 const stageError=row.kind==='sale_counts'?row.error==='Черновик продажи уже закрыт.':row.kind==='sale_selected'?row.error==='Сначала сохраните чек и количество кадров.':row.kind==='sale_complete'&&(row.error==='Продажа уже завершена или отменена.'||/^Сначала загрузите все выбранные фотографии: \d+\/\d+\.$/.test(row.error||''));
 // Old clients sent later sale stages after a receipt/photo failed. Recheck
 // only those exact failures once their original local predecessors succeeded.
 return Boolean(required&&stageError&&previous.some(step=>isSaleStart(step))&&previous.some(step=>step.kind===required)&&previous.every(step=>step.status==='synced'));
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
// This exact validation rolls back before recording the idempotency key. A
// deliberate new selection may replace its stale reference, never an uncertain
// send or an already accepted photo, receipt, count or completion operation.
export function canReplaceDiskSelection(row){
 return Boolean(row?.kind==='sale_disk_selected'&&row.status==='error'&&row.errorStatus===409&&!row.result&&
  row.error==='Папка съёмки изменилась. Откройте её и выберите фотографии заново.');
}
export async function replaceDiskSelection(key,photo,draftId){
 if(running||queueRepairing)throw new Error('Дождитесь завершения текущей отправки, затем выберите кадр заново.');
 queueRepairing=true;let committed=false;
 try{
  const actor=userId,generation=operationsGeneration,saleGeneration=salesGeneration;
  if(!Number.isSafeInteger(draftId)||draftId<=0||!photo||typeof photo.path!=='string'||!photo.path||
   typeof photo.revision!=='string'||!photo.revision)throw new Error('Откройте обновлённую папку и выберите один кадр заново.');
  let failure=null;
  try{await localAction('operations','readwrite',store=>{
   const request=store.get(key);
   const abort=error=>{failure=error;store.transaction.abort();};
   const correct=(row,start)=>{
    try{
     if(!actor||actor!==userId||generation!==operationsGeneration||saleGeneration!==salesGeneration||
      !row||row.userId!==actor||!canReplaceDiskSelection(row)||(row.generation||'')!==generation||
      (row.salesGeneration||'')!==saleGeneration)
      throw new Error('Этот кадр нельзя заменять: проверьте результат исходной отправки и обновите продажу.');
     const original=row.data?.draft;
     if(original?.id!==draftId&&!(original?.key&&start?.key===original.key&&start.userId===actor&&
      isSaleStart(start)&&start.status==='synced'&&start.result?.draftId===draftId&&
      (start.generation||'')===generation&&(start.salesGeneration||'')===saleGeneration))
      throw new Error('Откройте исходную продажу этого кадра.');
     if(photo.revision===row.data?.revision)throw new Error('Откройте обновлённую папку и выберите один кадр заново.');
     // Read eligibility and write the replacement in one IndexedDB transaction:
     // another tab's acknowledged/syncing result cannot be overwritten in between.
     store.put({...row,data:{...row.data,path:photo.path,revision:photo.revision},
      status:'local',error:'',errorStatus:0,retryAt:0});
    }catch(error){abort(error);}
   };
   request.onsuccess=()=>{
    const row=request.result;
    if(row?.data?.draft?.key){const start=store.get(row.data.draft.key);start.onsuccess=()=>correct(row,start.result);}
    else correct(row,null);
   };
   return request;
  });}catch(error){throw failure||error;}
  committed=true;changed();
 }finally{queueRepairing=false;if(committed||rerunRequested)void flushOutbox();}
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

export async function retryOperation(key){const rows=await outboxRows();if(!rows.some(r=>r.key===key))return;const row=await localAction('operations','readonly',s=>s.get(key));if(!row||row.userId!==userId)return;row.status='local';row.error='';if(!isBankGate(row)&&row.kind.startsWith('sale_')){row.saleTransientFailures=0;row.autoRetryPaused=false;}if(row.errorStatus!==429)row.retryAt=0;await update(row);for(const item of rows){if(item.errorStatus===424){const pending=await localAction('operations','readonly',s=>s.get(item.key));if(!pending||pending.userId!==userId)continue;pending.status='local';pending.error='';pending.retryAt=0;await update(pending);}}changed();return flushOutbox();}
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
  row.retryAt=0;if(!isBankGate(row)&&row.kind.startsWith('sale_')){row.saleTransientFailures=0;row.autoRetryPaused=false;}await update(row);
 }
 changed();return flushOutbox();
}
// A bank gate is a local queue stage, not a business operation. Persist the
// resolved immutable request before sending, then recover its bill by the same
// key after a lost response. Pending/test/declined bills cannot release files.
async function syncBankGate(row,active){
 const fail=(message,status=409)=>{const error=new Error(message);error.status=status;throw error;};
 const sameActor=()=>active()&&row.userId===userId&&(row.salesGeneration||'')===salesGeneration;
 if(!sameActor())fail('Данные продаж или аккаунт изменились. Обновите приложение.');
 if(!row.bankPayload){
  let bookingId=row.data.booking?.id,sourceDraftId=null;
  const reference=row.kind==='booking_bank_payment'?row.data.booking:row.data.draft;
  if(reference?.key){
   const dependency=await localAction('operations','readonly',store=>store.get(reference.key));
   if(!sameActor())return null;
   if(!dependency||dependency.userId!==row.userId||dependency.status!=='synced'){
    row.status='local';row.retryAt=Date.now()+1000;await update(row);scheduleRetry(1000);return null;
   }
   if(row.kind==='booking_bank_payment')bookingId=dependency.result?.bookingId;
   else{sourceDraftId=dependency.result?.draftId;bookingId=bookingId||dependency.result?.bookingId;}
  }else if(row.kind==='sale_bank_payment')sourceDraftId=reference?.id;
  if(!Number.isSafeInteger(bookingId)||bookingId<1||(row.kind==='sale_bank_payment'&&(!Number.isSafeInteger(sourceDraftId)||sourceDraftId<1)))fail('Сначала дождитесь подтверждения записи и черновика продажи.');
  row.bankPayload={bookingId,total:0,sold:0,tariffId:null,amount:null,...row.data.payment,
   ...(sourceDraftId?{sourceDraftId}:{}),purpose:row.kind==='booking_bank_payment'?'DEPOSIT':'PURCHASE',requestKey:row.key,
   ...(row.kind==='sale_bank_payment'?{allowZeroDue:true}:{})};
  if(!await update(row)||!sameActor())return null;
 }
 const bill=row.result?.id?await api(`/bank-payments/${row.result.id}/status`,{method:'POST',body:{},timeoutMs:20000}):
  await api('/bank-payments',{method:'POST',body:row.bankPayload,timeoutMs:20000});
 if(!sameActor())return null;
 if(bill?.status==='NO_PAYMENT_REQUIRED'&&row.kind==='sale_bank_payment'&&bill.allowZeroDue===true&&bill.amount==='0.00'&&
  bill.testMode===false&&bill.bookingId===row.bankPayload.bookingId&&bill.sourceDraftId===row.bankPayload.sourceDraftId)return bill;
 if(!bill||!Number.isSafeInteger(bill.id)||bill.id<1||bill.bookingId!==row.bankPayload.bookingId||
  (row.result?.id&&row.result.id!==bill.id))fail('Ответ банка не совпал с записью. Проверьте состояние счёта.');
 row.result=bill;
 // Save the bill identity before showing QR or scheduling another status call.
 if(!await update(row)||!sameActor())return null;
 if(bill.status==='PAID'&&bill.testMode===false)return bill;
 if(bill.status==='TEST_PAID'||(bill.status==='PAID'&&bill.testMode!==false))fail('Тестовая оплата: реальные поступления и продажа не подтверждены. Фотографии сохранены в очереди.');
 if(!['CREATING','PENDING'].includes(bill.status))fail(`Оплата не подтверждена (${bill.status}). Проверьте счёт перед продолжением.`);
 row.status='local';row.error='';row.errorStatus=0;row.retryAt=Date.now()+10000;
 await update(row);changed();scheduleRetry(10000);return null;
}
export async function flushOutbox(){
 if(!userId)return {state:'no-account'};
 if(!navigator.onLine)return {state:'offline'};
 if(queueRepairing){rerunRequested=true;return {state:'running'};}
 rerunRequested=false;
 const results=await Promise.all(['booking','attendance','bank','media'].map(flushOutboxWorker));
 const state=results.some(row=>row.state==='running')?'running':results.some(row=>row.state==='backoff')?'backoff':'done';
 return {state,completed:results.reduce((total,row)=>total+(row.completed||0),0),failed:results.reduce((total,row)=>total+(row.failed||0),0),temporary:results.reduce((total,row)=>total+(row.temporary||0),0)};
}
async function flushOutboxWorker(worker){
 if(!userId)return {state:'no-account'};
 if(!navigator.onLine)return {state:'offline'};
 if(workers.has(worker)){requestedWorkers.add(worker);return {state:'running'};}
 requestedWorkers.delete(worker);workers.add(worker);running=true;
 const actor=userId,generation=operationsGeneration,active=()=>actor===userId&&generation===operationsGeneration,summary={state:'done',completed:0,failed:0,temporary:0},blockedSales=new Set(),saleSteps=new Map(),blockedAttendance=new Set();
 try{
  // A tab may close after the correction was accepted but before its original
  // completion was reactivated. Recover that link on the next foreground pass.
  if(worker==='media')for(const repair of await outboxRows())if(repair.status==='synced'&&repair.resumeCompleteKey)await resumeRepairedCompletion(repair);
  for(const item of await outboxRows()){
   if(!active())break;
   if(operationWorker(item)!==worker)continue;
   // Legacy rows remain legacy packets; never stamp a freshly adopted reset
   // generation onto an old action, even if another tab held a stale cursor.
   if(item.kind!=='cash_expense'&&(item.generation||'')!==generation)continue;
   if(worker==='attendance'&&blockedAttendance.has(item.date))continue;
   // Booking now has a separate worker. Its receipt must still wait for the
   // exact original booking acknowledgement, rather than racing it into a 424.
   if(['booking_receipt','booking_bank_payment'].includes(item.kind)&&item.data?.booking?.key){
    const booking=await localAction('operations','readonly',store=>store.get(item.data.booking.key));
    if(!active())break;
    if(booking?.userId!==actor||booking.status!=='synced'||!booking.result?.bookingId)continue;
   }
   if(item.resumeByKey){const repair=await localAction('operations','readonly',store=>store.get(item.resumeByKey));if(repair?.status!=='synced')continue;}
   const sale=saleChain(item);
   // The bank worker owns its wait without holding the media worker. Every
   // later sale stage still waits for the same durable gate in IndexedDB.
   if(item.kind.startsWith('sale_')&&!isSaleStart(item)){
    const chain=await outboxRows(),gate=chain.find(step=>step.kind==='sale_bank_payment'&&saleChain(step)===sale);
    if(gate&&gate.key!==item.key&&gate.status!=='synced')continue;
   }
   if(sale&&blockedSales.has(sale))continue;
   const previous=sale?saleSteps.get(sale)||[]:[];
   if(sale&&!saleSteps.has(sale))saleSteps.set(sale,previous);
   if(!['local','syncing'].includes(item.status)&&!legacyMediaError(item)&&!renewedAuth(item)&&!legacySaleDependency(item,previous)){if(sale){previous.push(item);if(item.status!=='synced')blockedSales.add(sale);}continue;}
   const row=await localAction('operations','readonly',s=>s.get(item.key));
   if(!active())break;
   if(!row)continue;
   if(worker==='attendance'&&row.status==='synced')blockedAttendance.delete(row.date);
   if(!['local','syncing'].includes(row.status)&&!legacyMediaError(row)&&!renewedAuth(row)&&!legacySaleDependency(row,previous)){if(sale){previous.push(row);if(row.status!=='synced')blockedSales.add(sale);}continue;}
   if(legacySaleDependency(row,previous)){row.saleDependencyRecovery=SALE_DEPENDENCY_RECOVERY;row.status='local';row.error='';row.retryAt=0;await update(row);}
   if(sale)previous.push(row);
   if(legacyMediaError(row)){row.legacyMediaRecovered=true;row.legacyMediaRecovery=LEGACY_MEDIA_RECOVERY;row.status='local';row.error='';await update(row);}
   if(renewedAuth(row)){row.status='local';row.error='';row.retryAt=0;}
   // Hold later shift stages only for work that can still be accepted. A
   // definitive old checklist/photo rejection must not strand a fresh camera
   // retake or hide the server's explicit missing-start response forever.
   if(worker==='attendance')blockedAttendance.add(row.date);
   if(row.retryAt>Date.now()){if(sale)blockedSales.add(sale);scheduleRetry(row.retryAt-Date.now());summary.state='backoff';continue;}
   row.authEpoch=authEpoch;
   row.status='syncing';if(!await update(row))break;changed();
   try{
    if(!active())break;
    if(isBankGate(row)){
     const result=await syncBankGate(row,active);
     if(!active())break;
     if(!result){if(sale)blockedSales.add(sale);continue;}
     row.result=result;
    }else{
    const operation={actorId:row.userId,key:row.key,kind:row.kind,date:row.date,data:row.data,...(row.generation!==undefined?{generation:row.generation}:{}),...((row.kind.startsWith('sale_')||row.kind==='booking')&&row.salesGeneration?{salesGeneration:row.salesGeneration}:{})};
    let body=operation,path='/operations/sync';
    if(row.blob){
     const bytes=await row.blob.arrayBuffer();
     if(!bytes.byteLength){const error=new Error('Сохранённое фото пустое. Выберите файл заново.');error.status=400;throw error;}
     const upload=new Blob([bytes],{type:row.blob.type||'application/octet-stream'});
     body=new FormData();body.append('operation',JSON.stringify(operation));body.append('file',upload,row.filename||'upload');
     path='/operations/media';
    }
    // Disk selections also transfer original bytes server-side despite using
    // JSON. Keep their existing media allowance; only booking is a short call.
    row.result=await api(path,{method:'POST',body,timeoutMs:worker==='booking'?20000:180000});
    }
    if(!active())break;
    row.status='synced';row.error='';row.errorStatus=0;row.retryAt=0;row.blob=null;
    if(sale&&!isBankGate(row)){row.saleTransientFailures=0;row.autoRetryPaused=false;}
    summary.completed++;
   }catch(e){
    if(!active())break;
    if(sale)blockedSales.add(sale);
    // Missing dependencies and receipt failures must not strand independent shoot photos.
    // Keep their blobs on this device and retry without marking a dependent sale as failed.
    const temporary=!e.status||e.status>=500||[408,424,429].includes(e.status);
    const limitedSale=temporary&&sale&&!isBankGate(row);
    if(limitedSale)row.saleTransientFailures=(row.saleTransientFailures||0)+1;
    const paused=limitedSale&&row.saleTransientFailures>3;
    row.status=temporary?'local':'error';
    row.error=e.message;row.errorStatus=e.status||0;
    const backoff=limitedSale?[30000,60000,120000][Math.min(2,row.saleTransientFailures-1)]:30000;
    row.retryAt=temporary?Date.now()+Math.max(backoff,e.status===429?e.retryAfterMs||0:0):0;
    if(paused){row.status='error';row.autoRetryPaused=true;if(e.status!==429)row.retryAt=0;row.lastTransientError=e.message;row.error='Передача приостановлена после временных сбоев. Фото сохранены на устройстве. Нажмите «Продолжить отправку».';}
    await update(row);changed();
    if(temporary){
     summary.temporary++;
     if(!paused)scheduleRetry(Math.max(backoff,row.retryAt-Date.now()));
     if(sale||e.status===424||e.status>=500||['booking_receipt','sale_receipt'].includes(row.kind))continue;
     break;
    }
    summary.failed++;
    if(worker==='attendance')blockedAttendance.delete(row.date);
    if(e.status===401)break;
    continue;
   }
   await update(row);changed();if(row.resumeCompleteKey)await resumeRepairedCompletion(row);
   if(worker==='attendance')blockedAttendance.delete(row.date);
   if(row.kind==='booking'){void flushOutboxWorker('media');void flushOutboxWorker('bank');}
   if(isSaleStart(row))void flushOutboxWorker('bank');
   if(row.kind==='sale_bank_payment')void flushOutboxWorker('media');
  }
 }finally{workers.delete(worker);running=workers.size>0;changed();if(userId&&(!active()||requestedWorkers.has(worker)))void flushOutboxWorker(worker);}
 return summary;
}
window.addEventListener('online',()=>flushOutbox());
// Mobile browsers suspend timers in the background even while onLine stays true.
for(const event of ['focus','pageshow'])window.addEventListener(event,()=>flushOutbox());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void flushOutbox();});
window.addEventListener('pb-operations-reset',event=>{if(event.detail?.userId===userId){operationsGeneration=event.detail.generation;clearTimeout(timer);timer=null;retryDue=0;rerunRequested=false;requestedWorkers.clear();}});
