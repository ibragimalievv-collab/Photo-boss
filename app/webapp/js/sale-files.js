// Compare file contents, so renamed copies cannot masquerade as new sold frames.
export async function checkSaleFiles(files){
 const seen=new Set();
 for(const file of files){
  const digest=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
  seen.add(Array.from(new Uint8Array(digest),n=>n.toString(16).padStart(2,'0')).join(''));
 }
 if(seen.size<files.length)throw new Error(`Выбрано файлов: ${files.length}, разных фотографий: ${seen.size}. Уберите повторные копии и выберите недостающие кадры. Проданное количество указывайте по фактической покупке.`);
}

// A chosen FileList is only a selection. A ready gallery requires the final
// durable operation acknowledgement and every predecessor in that same sale.
export function saleSaveState(rows){
 const files=rows.filter(row=>['sale_selected','sale_disk_selected'].includes(row?.kind));
 const complete=rows.find(row=>row?.kind==='sale_complete');
 const accepted=Boolean(rows.length&&rows.every(row=>row?.status==='synced')&&
  Number.isSafeInteger(complete?.result?.saleId)&&complete.result.saleId>0);
 const gallery=accepted?complete.result.gallery:null;
 return {accepted,ready:Boolean(accepted&&gallery?.saleId===complete.result.saleId&&gallery.ready===true&&gallery.state==='ready'&&
  typeof gallery.clientUrl==='string'&&gallery.clientUrl),gallery,
  files:files.length,transferred:files.filter(row=>row.status==='synced').length,
  failed:rows.find(row=>row?.error)||null,result:accepted?complete.result:null};
}
