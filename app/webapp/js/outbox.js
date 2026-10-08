import {api} from './api.js';
import {localAction,openLocal} from './localstore.js';
let userId=null,running=false,timer=null;
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
export async function retryOperation(key){const rows=await outboxRows();if(!rows.some(r=>r.key===key))return;const row=await localAction('operations','readonly',s=>s.get(key));if(!row||row.userId!==userId)return;row.status='local';row.error='';await update(row);for(const item of rows){if(item.errorStatus===424){const pending=await localAction('operations','readonly',s=>s.get(item.key));if(!pending||pending.userId!==userId)continue;pending.status='local';pending.error='';await update(pending);}}changed();return flushOutbox();}
export async function flushOutbox(){
 if(running||!userId||!navigator.onLine)return;running=true;const actor=userId;
 try{for(const item of await outboxRows()){
  if(actor!==userId)break;if(!['local','syncing'].includes(item.status))continue;
  const row=await localAction('operations','readonly',s=>s.get(item.key));
  if(actor!==userId)break;if(!row||!['local','syncing'].includes(row.status))continue;
  row.status='syncing';await update(row);changed();
  try{
   const operation={actorId:row.userId,key:row.key,kind:row.kind,date:row.date,data:row.data};
   let body=operation,path='/operations/sync';
   if(row.blob){const bytes=await row.blob.arrayBuffer();if(!bytes.byteLength){const error=new Error('Сохранённое фото пустое. Выберите файл заново.');error.status=400;throw error;}const upload=new Blob([bytes],{type:row.blob.type||'application/octet-stream'});body=new FormData();body.append('operation',JSON.stringify(operation));body.append('file',upload,row.filename||'upload');path='/operations/media';}
   row.result=await api(path,{method:'POST',body,timeoutMs:180000});row.status='synced';row.error='';row.errorStatus=0;row.blob=null;
  }catch(e){
   row.status=(!e.status||e.status>=500)?'local':'error';row.error=e.message;row.errorStatus=e.status||0;
   await update(row);changed();
   if(!e.status||e.status>=500){clearTimeout(timer);timer=setTimeout(()=>flushOutbox(),30000);break;}
   if([401,403].includes(e.status))break;continue;
  }
  await update(row);changed();
 }}finally{running=false;}
}
window.addEventListener('online',()=>flushOutbox());

