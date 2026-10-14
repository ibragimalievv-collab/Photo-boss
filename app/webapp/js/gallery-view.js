/* Shared approved prototype layout for staff and the protected guest gallery. */
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export {esc};
export function galleryMarkup(data,{staff=false,back='К проектам'}={}){
 const cover=data.photos.find(p=>p.id===data.coverId)||data.photos[0];
 return `<section class="gallery"><header class="gbar"><div class="gbrand">PHOTO BOSS</div><div>${staff?`<button class="gbutton" data-gv="back">← ${esc(back)}</button>`:''}<button class="gbutton" data-gv="share">↗ Поделиться</button></div></header><div class="hero">${cover?`<img data-gallery-image="${cover.id}" alt="Обложка галереи">`:'<div class="hero-empty"></div>'}<div class="herotxt"><small>ПЕРСОНАЛЬНАЯ ГАЛЕРЕЯ</small><h1>${esc(data.title)}</h1><div>${esc(data.date||'')} ${esc(data.time||'')} · ${data.photos.length} фотографий · Photo Boss</div></div></div><main class="gallerymain"><div class="ghead"><div><h2>Фотографии гостей</h2><p>Отметьте любимые кадры или скачайте оригиналы</p></div><div class="pills"><button class="pill active" data-gv-filter="all">Все фото</button><button class="pill" data-gv-filter="fav">Избранное <span data-fav-count>${data.photos.filter(p=>p.favorite).length}</span></button></div></div><div class="project-actions"><button class="ghost" data-gv="download-all">↓ Скачать весь альбом ZIP</button><button class="ghost" data-gv="download-fav">↓ Скачать избранное</button>${staff?'<button class="ghost" data-gv="upload">＋ Добавить свои фото</button><button class="ghost" data-gv="settings">⚙ Настройки галереи</button>':''}</div><p class="gallery-status" role="status"></p><div class="grid" data-gallery-grid></div></main></section>`;
}
export function mountGallery(root,data,callbacks={}){
 let filter='all',current=0,disposed=false,viewer=null,observer=null,active=0;const urls=new Map(),queue=[],waiting=new Set();
 root.innerHTML=galleryMarkup(data,{staff:callbacks.staff});
 const status=text=>{const el=root.querySelector('.gallery-status');if(el)el.textContent=text;};
 async function imageUrl(photo,full=false){
  const key=photo.id+':'+full;if(urls.has(key))return urls.get(key);
  const promise=Promise.resolve(callbacks.image(photo,full)).then(value=>{if(disposed){if(value.startsWith('blob:'))URL.revokeObjectURL(value);throw Error('Галерея закрыта.');}return value;});urls.set(key,promise);
  try{return await promise;}catch(e){urls.delete(key);throw e;}
 }
 async function pump(){
  while(!disposed&&active<4&&queue.length){const photo=queue.shift();active++;(async()=>{try{const url=await imageUrl(photo);if(!disposed)for(const img of root.querySelectorAll(`[data-gallery-image="${photo.id}"]`))img.src=url;}catch{if(!disposed)for(const img of root.querySelectorAll(`[data-gallery-image="${photo.id}"]`))img.alt='Фото временно недоступно';}finally{waiting.delete(photo.id);active--;pump();}})();}
 }
 function requestImage(id){if(waiting.has(id))return;const photo=data.photos.find(p=>p.id===id);if(photo){waiting.add(id);queue.push(photo);pump();}}
 function watch(){observer?.disconnect();observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){requestImage(Number(entry.target.dataset.galleryImage));observer.unobserve(entry.target);}}, {rootMargin:'400px'});for(const img of root.querySelectorAll('[data-gallery-image]'))observer.observe(img);}
 function grid(){
  const photos=data.photos.filter(p=>filter!=='fav'||p.favorite);
  root.querySelector('[data-gallery-grid]').innerHTML=photos.map(p=>`<figure class="photo"><button data-gv-photo="${p.id}" aria-label="Открыть ${esc(p.name)}"><img data-gallery-image="${p.id}" alt="${esc(p.name)}" loading="lazy"></button><button class="round heart ${p.favorite?'on':''}" data-gv-favorite="${p.id}" aria-label="${p.favorite?'Убрать из избранного':'В избранное'} ${esc(p.name)}" aria-pressed="${!!p.favorite}">${p.favorite?'♥':'♡'}</button><figcaption>${esc(p.name)}</figcaption></figure>`).join('')||'<div class="empty">'+(filter==='fav'?'Пока нет избранных фотографий':'В этой съёмке ещё нет фотографий')+'</div>';
  root.querySelector('[data-fav-count]').textContent=data.photos.filter(p=>p.favorite).length;watch();
 }
 async function favorite(p,button){button.disabled=true;try{const next=!p.favorite;await callbacks.favorite(p,next);if(disposed)return;p.favorite=next;grid();if(viewer?.open)updateViewerHeart();}catch(e){status(e.message);}finally{button.disabled=false;}}
 function visible(){return data.photos.filter(p=>filter!=='fav'||p.favorite);}
 function updateViewerHeart(){const p=visible()[current],button=viewer.querySelector('[data-v="favorite"]');button.textContent=p?.favorite?'♥ Избранное':'♡ В избранное';button.disabled=!p;}
 async function showPhoto(index){
  const rows=visible();if(!rows.length)return;current=(index+rows.length)%rows.length;
  if(!viewer){viewer=document.createElement('dialog');viewer.className='viewer';viewer.innerHTML='<button class="gbutton vdownload" data-v="download">↓ Скачать</button><button class="gbutton vfavorite" data-v="favorite"></button>'+(callbacks.cover?'<button class="gbutton vcover" data-v="cover">На обложку</button>':'')+'<button class="gbutton vclose" data-v="close">Закрыть ×</button><button class="nav prev" data-v="prev" aria-label="Предыдущее фото">‹</button><img alt=""><button class="nav next" data-v="next" aria-label="Следующее фото">›</button>';root.append(viewer);viewer.addEventListener('click',async e=>{const button=e.target.closest('[data-v]');if(!button)return;const action=button.dataset.v,p=visible()[current];try{if(action==='close')viewer.close();if(action==='next')await showPhoto(current+1);if(action==='prev')await showPhoto(current-1);if(action==='download'&&p)await callbacks.download([p]);if(action==='favorite'&&p)await favorite(p,button);if(action==='cover'&&p){button.disabled=true;await callbacks.cover(p);data.coverId=p.id;const image=root.querySelector('.hero [data-gallery-image]');if(image){image.dataset.galleryImage=p.id;image.src=await imageUrl(p);}status('Обложка сохранена.');}}catch(error){status(error.message);}finally{button.disabled=false;}});viewer.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){e.preventDefault();showPhoto(current-1);}if(e.key==='ArrowRight'){e.preventDefault();showPhoto(current+1);}});}
  if(!viewer.open)viewer.showModal();const p=rows[current];updateViewerHeart();const image=viewer.querySelector('img');image.alt=p.name;image.removeAttribute('src');const ticket=p.id;try{const url=await imageUrl(p,true);if(!disposed&&visible()[current]?.id===ticket)image.src=url;}catch(e){status(e.message);}
 }
 async function click(e){const button=e.target.closest('button');if(!button||button.disabled||button.closest('.viewer'))return;
  if(button.dataset.gvFavorite){const p=data.photos.find(p=>p.id===Number(button.dataset.gvFavorite));if(p)return favorite(p,button);}
  if(button.dataset.gvPhoto){return showPhoto(visible().findIndex(p=>p.id===Number(button.dataset.gvPhoto)));}
  if(button.dataset.gvFilter){filter=button.dataset.gvFilter;for(const b of root.querySelectorAll('[data-gv-filter]'))b.classList.toggle('active',b===button);grid();return;}
  const action=button.dataset.gv;if(!action)return;button.disabled=true;try{if(action==='back')callbacks.back();if(action==='share')await callbacks.share();if(action==='upload')callbacks.upload();if(action==='settings')callbacks.settings();if(action==='download-all')await callbacks.download(data.photos);if(action==='download-fav')await callbacks.download(data.photos.filter(p=>p.favorite));}catch(error){status(error.message);}finally{button.disabled=false;}
 }
 root.addEventListener('click',click);grid();
 return ()=>{disposed=true;observer?.disconnect();root.removeEventListener('click',click);if(viewer?.open)viewer.close();viewer?.remove();for(const promise of urls.values())promise.then(url=>{if(url.startsWith('blob:'))URL.revokeObjectURL(url);}).catch(()=>{});};
}
export function saveBlob(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}
export async function copyLink(url){if(!url)throw Error('Откройте доступ гостю в настройках галереи.');try{await navigator.clipboard.writeText(url);}catch{const input=document.createElement('textarea');input.value=url;document.body.append(input);input.select();const copied=document.execCommand('copy');input.remove();if(!copied)throw Error('Не удалось скопировать ссылку. Откройте настройки проекта.');}}
export async function shareLink(url){if(!url)throw Error('Откройте доступ гостю в настройках галереи.');if(navigator.share){try{await navigator.share({title:'Photo Boss',url});return;}catch(e){if(e.name==='AbortError')return;}}await copyLink(url);}
