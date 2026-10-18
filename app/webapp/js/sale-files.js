// Compare file contents, so renamed copies cannot masquerade as new sold frames.
export async function checkSaleFiles(files){
 const seen=new Set();
 for(const file of files){
  const digest=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
  seen.add(Array.from(new Uint8Array(digest),n=>n.toString(16).padStart(2,'0')).join(''));
 }
 if(seen.size<files.length)throw new Error(`Выбрано файлов: ${files.length}, разных фотографий: ${seen.size}. Уберите повторные копии и выберите недостающие кадры. Проданное количество указывайте по фактической покупке.`);
}
