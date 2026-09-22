import {api} from './api.js';
import {localAction} from './localstore.js';
let userId=null,running=false,timer=null;
const update=row=>localAction('operations','readwrite',s=>s.put(row));
const changed=()=>window.dispatchEvent(new Event('pb-outbox'));
export function setOutboxUser(id){userId=id;void flushOutbox();}
export async function outboxRows(){if(!userId)return [];return (await localAction('operations','readonly',s=>s.getAll())).filter(r=>r.userId===userId).sort((a,b)=>a.createdAt-b.createdAt||(a.order||0)-(b.order||0));}
export async function enqueueBatch(items){
 if(!userId)throw new Error('Сначала откройте приложение при доступной сети.');
 if(!items.length)throw new Error('Нет действий для сохранения.');
 const at=Date.now(),actor=userId;
 const rows=items.map((item,i)=>({key:item.key||crypto.randomUUID(),userId:actor,kind:item.kind,date:item.date,data:item.data,blob:item.blob||null,filename:item.blob?.name||'',status:'local',createdAt:at,order:i}));
 await localAction('operations','readwrite',s=>{for(const row of rows)s.add(row);});
 changed();void flushOutbox();return rows;
}
export async function enqueueOperation(kind,date,data,blob=null){return (await enqueueBatch([{kind,date,data,blob}]))[0];}
export async function retryOperation(key){const rows=await outboxRows(),row=rows.find(r=>r.key===key);if(!row)return;row.status='local';row.error='';await update(row);for(const item of rows){if(item.errorStatus===424){item.status='local';item.error='';await update(item);}}changed();return flushOutbox();}
export async function flushOutbox(){
 if(running||!userId||!navigator.onLine)return;running=true;const actor=userId;
 try{for(const row of await outboxRows()){
  if(actor!==userId)break;if(!['local','syncing'].includes(row.status))continue;
  row.status='syncing';await update(row);changed();
  try{
   const operation={actorId:row.userId,key:row.key,kind:row.kind,date:row.date,data:row.data};
   let body=operation,path='/operations/sync';
   if(row.blob){body=new FormData();body.append('operation',JSON.stringify(operation));body.append('file',row.blob,'upload');path='/operations/media';}
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
