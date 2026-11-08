const LIMIT=8*1024*1024,MAGIC=new TextEncoder().encode('PBRCPT1\n');
const types=new Set(['image/jpeg','image/png','image/webp','application/pdf']);
const states=new WeakMap(),bound=new Set();
export function validateReceiptFiles(files,{empty=false}={}){
 if(!Array.isArray(files)||files.length>2||(!empty&&!files.length))throw Error('Выберите один или два файла чека.');
 for(const file of files)if(!file||!types.has(file.type)||!file.size||file.size>LIMIT)throw Error('Каждый файл: JPEG, PNG, WebP или одностраничный PDF до 8 МБ.');
 return files;
}
export function receiptFiles(input){return states.get(input)?.files||[...(input?.files||[])];}
export async function receiptPayload(files){
 validateReceiptFiles(files);
 const unique=new Map();
 for(const file of files){const bytes=await file.arrayBuffer();if(bytes.byteLength!==file.size)throw Error('Не удалось прочитать вложение чека. Выберите его заново.');const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');unique.set(hash,{bytes,type:file.type});}
 const ordered=[...unique.entries()].sort(([a],[b])=>a.localeCompare(b));
 if(ordered.length===1)return new Blob([ordered[0][1].bytes],{type:ordered[0][1].type});
 const header=new TextEncoder().encode(JSON.stringify(ordered.map(([sha256,{bytes}])=>({sha256,size:bytes.byteLength}))));
 const size=new Uint8Array(4);new DataView(size.buffer).setUint32(0,header.byteLength);
 return new Blob([MAGIC,size,header,...ordered.map(([,value])=>value.bytes)],{type:'application/vnd.photoboss.receipt-bundle'});
}
function syncNative(input,state){try{const transfer=new DataTransfer();for(const file of state.files)transfer.items.add(file);input.files=transfer.files;}catch{/* The owned selection remains readable through receiptFiles. */}}
function render(input,state){
 for(const url of state.urls)URL.revokeObjectURL(url);state.urls=[];state.view.replaceChildren();
 const count=document.createElement('p');count.textContent=`Файлов чека: ${state.files.length} из 2. Можно добавить вторую часть того же чека.`;state.view.append(count);
 state.files.forEach((file,index)=>{
  const row=document.createElement('div'),name=document.createElement('p');name.textContent=`${index+1}. ${file.name||'Чек'} · ${Math.ceil(file.size/1024)} КБ`;row.append(name);
  const url=URL.createObjectURL(file);state.urls.push(url);
  if(file.type.startsWith('image/')){const image=document.createElement('img');image.src=url;image.alt=`Вложение чека ${index+1}`;image.style.maxWidth='160px';image.style.maxHeight='160px';row.append(image);}
  else{const link=document.createElement('a');link.href=url;link.target='_blank';link.rel='noopener';link.textContent='Просмотреть PDF';row.append(link);}
  for(const [label,action] of [['Заменить',()=>{state.replacing=index;input.click();}],['Удалить',()=>{state.files.splice(index,1);syncNative(input,state);render(input,state);}]]){const button=document.createElement('button');button.type='button';button.className='btn ghost';button.textContent=`${label} файл ${index+1}`;button.onclick=action;row.append(button);}
  state.view.append(row);
 });
 const error=document.createElement('p');error.setAttribute('role','alert');error.textContent=state.error||'';state.view.append(error);
}
export function bindReceiptInput(input){
 if(states.has(input))return;
 input.multiple=true;input.accept=[...types].join(',');
 const view=document.createElement('div');view.dataset.receiptFiles='';input.after(view);
 const state={files:[...(input.files||[])],urls:[],view,replacing:null,error:''};states.set(input,state);bound.add(input);render(input,state);
 input.form?.addEventListener('reset',()=>{state.files=[];state.error='';state.replacing=null;render(input,state);});
 input.addEventListener('change',()=>{
  const selected=[...(input.files||[])];if(!selected.length)return;
  try{
   if(state.replacing!=null&&selected.length!==1)throw Error('Для замены выберите один файл.');const next=state.replacing==null?[...state.files,...selected]:state.files.map((file,i)=>i===state.replacing?selected[0]:file);
   validateReceiptFiles(next,{empty:true});state.files=next;state.error='';
  }catch(error){state.error=error.message;}
  state.replacing=null;syncNative(input,state);render(input,state);
 });
}
function bindRoot(root){
 const selector='input[type=file][name=receipt],input[type=file][data-receipt-input]';
 if(root.matches?.(selector))bindReceiptInput(root);
 for(const input of root.querySelectorAll?.(selector)||[])bindReceiptInput(input);
}
if(typeof document!=='undefined'&&typeof MutationObserver!=='undefined'&&document.documentElement){
 bindRoot(document);
 new MutationObserver(records=>{
  for(const record of records)for(const node of record.addedNodes)if(node.nodeType===1)bindRoot(node);
  for(const input of bound)if(!input.isConnected){for(const url of states.get(input).urls)URL.revokeObjectURL(url);bound.delete(input);}
 }).observe(document.documentElement,{childList:true,subtree:true});
}
