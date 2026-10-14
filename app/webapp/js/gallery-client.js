import {mountGallery,shareLink} from './gallery-view.js';
const base=location.pathname.replace(/\/$/,''),root=document.querySelector('#guestGallery');
async function request(path,options={}){const r=await fetch(base+path,{credentials:'same-origin',cache:'no-store',...options});if(!r.ok)throw Error('Галерея недоступна. Обновите страницу и проверьте срок доступа.');return r.json();}
try{const data=await request('/data');mountGallery(root,data,{
 image:(p,full)=>Promise.resolve(p.url+(full?'':'?preview=1')),
 favorite:(p,value)=>request(`/photos/${p.id}/favorite`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({favorite:value})}),
 share:()=>shareLink(data.clientUrl),
 download:async photos=>{if(!photos.length)throw Error('Сначала отметьте любимые кадры.');const a=document.createElement('a');a.href=photos.length===1?photos[0].url+'?download=1':base+'/album.zip'+(photos.length===data.photos.length?'':'?ids='+photos.map(p=>p.id).join(','));a.download='Photo-Boss';a.click();}
});}catch(error){const note=document.createElement('p');note.textContent=error.message;root.prepend(note);}
