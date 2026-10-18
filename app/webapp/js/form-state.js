/* Drafts belong to the authenticated account. Never store passwords or tokens.
 * Text survives a reload in this tab. File handles survive a redraw only;
 * after submission their bytes are durably owned by the existing outbox. */
let actor=null,generation='',operationalReset=false;
const drafts=new Map();
const allowed=new Set(['workflowBooking','workflowSale','workflowShoot','salesTrainingForm','bookingTrainingForm']);
const operational=new Set(['workflowBooking','workflowSale','workflowShoot']);
const prefix='pb-form-draft-v1:';
const ttl=7*86400000;
function key(form){return actor&&allowed.has(form.id)&&!(operationalReset&&operational.has(form.id))?`${prefix}${actor}:${form.id}:${form.dataset?.draftScope||'default'}${operational.has(form.id)&&generation?':'+generation:''}`:null;}
function saved(k){if(drafts.has(k))return drafts.get(k);try{const d=JSON.parse(sessionStorage.getItem(k)||'null');if(d&&Date.now()-d.at<ttl){drafts.set(k,d);return d;}}catch{}return null;}
function persist(k,d){drafts.set(k,d);try{sessionStorage.setItem(k,JSON.stringify({...d,files:undefined}));}catch{}}
export function setFormActor(id,nextGeneration=''){
 const nextActor=Number.isSafeInteger(id)&&id>0?id:null;
 const next=typeof nextGeneration==='string'?nextGeneration:'';
 if(actor!==nextActor){drafts.clear();operationalReset=false;}
 actor=nextActor;generation=next;
 if(!actor||!generation)return;
 try{
  const marker=`pb-form-generation-v1:${actor}`;
  if(sessionStorage.getItem(marker)===generation)return;
  for(const k of [...drafts.keys()])if([...operational].some(form=>k.startsWith(`${prefix}${actor}:${form}:`)))drafts.delete(k);
  for(let i=sessionStorage.length-1;i>=0;i--){const k=sessionStorage.key(i);if(k&&[...operational].some(form=>k.startsWith(`${prefix}${actor}:${form}:`)))sessionStorage.removeItem(k);}
  sessionStorage.removeItem(`pb-workflow-sale:${actor}`);sessionStorage.setItem(marker,generation);
 }catch{}
}
export function captureForm(form){
 const k=key(form);if(!k)return;
 const fields={},files={},names={};
 for(const el of [...form.elements]){
  if(!el.name||['button','submit','password','hidden'].includes(el.type))continue;
  if(el.type==='file'){files[el.name]=[...el.files];names[el.name]=files[el.name].map(f=>f.name);}
  else if(el.type==='checkbox')fields[el.name]=el.checked;
  else if(el.type==='radio'){if(el.checked)fields[el.name]=el.value;}
  else fields[el.name]=el.value;
 }
 const previous=saved(k);
 for(const name of Object.keys(names))if(!names[name].length&&previous?.names?.[name]?.length&&!files[name]?.length&&form.dataset.pendingKeys){names[name]=previous.names[name];}
 persist(k,{at:Date.now(),fields,files,names,pendingKeys:form.dataset.pendingKeys||null});
}
export function captureForms(root=document){for(const form of root.querySelectorAll('form'))captureForm(form);}
export function clearForm(form){const k=key(form);if(k){drafts.delete(k);try{sessionStorage.removeItem(k);}catch{}}delete form.dataset.pendingKeys;}
export function restoreForms(root=document){
 for(const form of root.querySelectorAll('form')){
  const k=key(form),d=k&&saved(k);if(!d)continue;
  if(d.pendingKeys)form.dataset.pendingKeys=d.pendingKeys;
  for(const [name,value] of Object.entries(d.fields||{})){
   const el=form.elements[name];if(!el||el.readOnly)continue;
   if(el.type==='checkbox')el.checked=Boolean(value);
   else if(el.type!=='file'&&el.type!=='password'){
    if(el.tagName==='SELECT'&&![...el.options].some(o=>o.value===String(value)))continue;
    el.value=value;
   }
  }
  const missing=[];
  for(const [name,names] of Object.entries(d.names||{})){
   const el=form.elements[name];if(!el||el.type!=='file'||el.files?.length)continue;
   const files=d.files?.[name];
   if(files?.length){try{const dt=new DataTransfer();for(const f of files)dt.items.add(f);el.files=dt.files;}catch{}}
   if(names.length&&!el.files?.length&&!d.pendingKeys)missing.push(`${name}: ${names.length}`);
  }
  if(missing.length&&!form.querySelector('[data-draft-warning]')){
   const note=document.createElement('p');note.dataset.draftWarning='';note.setAttribute('role','status');
   note.textContent='Поля черновика восстановлены. Неотправленные файлы после перезагрузки выберите заново ('+missing.join(', ')+'). Уже отправленные файлы остаются в очереди.';form.append(note);
  }
 }
}
for(const event of ['input','change'])document.addEventListener(event,e=>{const form=e.target?.closest?.('form');if(form)captureForm(form);});
window.addEventListener('pagehide',()=>captureForms());
document.addEventListener('pb-chat-account-reset',()=>setFormActor(null));
window.addEventListener('pb-operations-reset',event=>{const sameActor=actor===event.detail?.userId;setFormActor(event.detail?.userId,event.detail?.generation);if(sameActor)operationalReset=true;});
