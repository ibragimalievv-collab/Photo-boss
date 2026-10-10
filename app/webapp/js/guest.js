const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tg=window.Telegram?.WebApp;
let me=null,gallery=null,draft=null,requestKey=null,pollTimer=null,activeFilmId=null;
tg?.ready();tg?.expand();
function message(text){$('#message').textContent=text;}
async function api(path,{method='GET',body,raw=false}={}){
 const headers={'X-PhotoBoss-Guest':'1'};
 if(tg?.initData)headers['X-Telegram-Init-Data']=tg.initData;
 if(body!==undefined&&!raw)headers['Content-Type']='application/json';
 const response=await fetch('/api/guest'+path,{method,headers,body:body===undefined?undefined:raw?body:JSON.stringify(body),credentials:'same-origin',cache:'no-store',redirect:'error'});
 const data=await response.json();if(!response.ok){const error=Error(data.error||'Не удалось выполнить запрос.');error.status=response.status;throw error;}return data;
}
async function loginView(){
 clearTimeout(pollTimer);me=null;gallery=null;draft=null;requestKey=null;
 $('#content').innerHTML='<h1>Ваши семейные моменты</h1><p class="muted">Все доступные вам альбомы — в одном аккаунте Photo Boss.</p><section class="narrow"><h2>Войти</h2><form id="login"><label>Логин<input name="login" autocomplete="username" inputmode="numeric" maxlength="16" required></label><label>Пароль<input name="password" type="password" autocomplete="current-password" minlength="10" maxlength="128" required></label><button>Войти</button></form><p>Первый вход и восстановление: подтвердите аккаунт через Telegram и откройте кнопку «Личный кабинет».</p><a id="telegramLogin" class="button secondary" hidden>Подтвердить аккаунт</a></section>';
 try{const config=await api('/config');$('#telegramLogin').href=config.telegramUrl;$('#telegramLogin').hidden=false;}catch(e){message(e.message);}
}
async function boot(){
 try{me=await api('/me');}catch(e){if(e.status===401&&tg?.initData){try{await api('/session',{method:'POST',body:{}});me=await api('/me');}catch(err){await loginView();message(err.message);return;}}else if(e.status===401){await loginView();return;}else{message(e.message);return;}}
 $('#content').innerHTML=`<h1>Ваши фотографии, ${esc(me.name)}</h1><div class="row"><button data-action="refresh" class="secondary">Обновить альбомы</button><button data-action="logout" class="secondary">Выйти</button></div><section><h2>Мои альбомы</h2><div class="albums">${me.albums.map(a=>`<button class="secondary" data-album="${a.id}">${esc(a.title)}${a.locked?' · пароль':''}</button>`).join('')||'<p>Альбомов пока нет. Сохраните альбом по приглашению фотографа.</p>'}</div><div id="album"></div></section><div id="editor"></div><section><h2>Семейные ролики</h2><p class="muted">Слайд-шоу из ваших фотографий: вертикально для телефона или горизонтально для телевизора. Можно добавить готовую песню. Генерация песни с помощью ИИ ещё не подключена.</p>${me.videoAvailable?'':'<p>Создание видео пока недоступно на сервере.</p>'}<div id="films" class="films"></div></section><section class="narrow"><h2>Вход по паролю</h2><p>Ваш логин: <strong>${esc(me.login)}</strong>. Настройте пароль для входа в браузере.</p><form id="password">${me.passwordConfigured?'<label>Текущий пароль<input name="currentPassword" type="password" autocomplete="current-password" maxlength="128"></label>':''}<label>Новый пароль<input name="password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required></label><label>Повторите пароль<input name="repeat" type="password" autocomplete="new-password" minlength="10" maxlength="128" required></label><button>Сохранить пароль</button></form></section>`;
 await loadFilms();
}
async function openAlbum(id){
 gallery=me.albums.find(a=>a.id===Number(id));draft=null;requestKey=null;$('#editor').innerHTML='';
 if(gallery.locked){$('#album').innerHTML=`<form id="unlock" class="narrow"><label>Пароль альбома<input name="password" type="password" autocomplete="current-password" maxlength="100" required></label><button>Открыть альбом</button></form>`;return;}
 const data=await api(`/galleries/${gallery.id}/photos`);
 $('#album').innerHTML=`<h3>${esc(gallery.title)}</h3><p>Выберите от 2 до 12 фотографий для слайд-шоу.</p><form id="createFilm"><div class="grid">${data.photos.map(p=>`<label class="photo"><img loading="lazy" src="${esc(p.url)}?preview=1" alt="${esc(p.name)}"><span><input type="checkbox" name="photo" value="${p.id}"> ${esc(p.name)}</span><a href="${esc(p.url)}" download>Скачать фото</a></label>`).join('')}</div><label>Название ролика<input name="title" value="Наш семейный отпуск" maxlength="100" required></label><label>Имена членов семьи<input name="names" maxlength="300" placeholder="По желанию"></label><label>Семейная история<textarea name="story" maxlength="1000" placeholder="Воспоминания и повод для ролика"></textarea></label><label>Формат<select name="format"><option value="VERTICAL">Вертикальный · 9:16</option><option value="HORIZONTAL">Горизонтальный · 16:9</option></select></label><button ${me.videoAvailable?'':'disabled'}>Подготовить слайд-шоу</button></form>`;
}
function renderDraft(){
 $('#editor').innerHTML=`<section><h2>${esc(draft.title)}</h2><form id="renderFilm"><label>Черновик текста песни<textarea name="lyrics" maxlength="2000">${esc(draft.lyrics)}</textarea></label><p class="muted">Это шаблон для редактирования. Текст сохраняется в заказе; автоматическое исполнение песни ещё не подключено.</p><label>Готовая музыка · по желанию<input type="file" name="audio" accept="audio/mpeg,audio/mp4,audio/wav,audio/x-wav"></label><p class="muted">MP3, M4A или WAV до 12 МБ. Без музыки получится слайд-шоу без звука.</p><label><input type="checkbox" name="confirmed" required> Подтверждаю фотографии и право использовать выбранную музыку.</label><div class="row"><button>Создать MP4</button><button type="button" data-cancel="${draft.id}" class="secondary">Отменить</button></div></form></section>`;
 $('#editor').scrollIntoView({behavior:'smooth',block:'start'});
}
const statuses={DRAFT:'Черновик',QUEUED:'В очереди',RENDERING:'Создаётся',READY:'Готов',FAILED:'Ошибка',CANCELLED:'Отменён'};
async function loadFilms(){
 clearTimeout(pollTimer);if(!me||!$('#films'))return;
 try{const data=await api('/films');const active=data.films.find(f=>f.id===activeFilmId);if(active?.status==='READY'){message('Ролик готов. Его можно скачать.');activeFilmId=null;}else if(active?.status==='FAILED'){message(active.error);activeFilmId=null;}$('#films').innerHTML=data.films.map(f=>`<article class="panel"><h3>${esc(f.title)}</h3><p>${statuses[f.status]||esc(f.status)} ${f.error?esc(f.error):''}</p>${f.url?`<video controls preload="none" src="${esc(f.url)}"></video><a class="button" href="${esc(f.url)}?download=1">Скачать MP4</a>`:''}${f.status==='DRAFT'?`<button class="secondary" data-draft="${f.id}">Продолжить</button>`:''}${['DRAFT','QUEUED','RENDERING','FAILED'].includes(f.status)?` <button class="secondary" data-cancel="${f.id}">Отменить</button>`:''}</article>`).join('')||'<p>Выберите фотографии в альбоме и создайте первый ролик.</p>';if(data.films.some(f=>['QUEUED','RENDERING'].includes(f.status)))pollTimer=setTimeout(loadFilms,4000);}catch(e){if(e.status===401){await loginView();}message(e.message);}
}
document.addEventListener('change',e=>{if(e.target.closest('#createFilm'))requestKey=null;});
document.addEventListener('submit',async e=>{
 const form=e.target;if(!['login','password','unlock','createFilm','renderFilm'].includes(form.id))return;e.preventDefault();const button=form.querySelector('button');button.disabled=true;message('');
 try{const data=new FormData(form);
  if(form.id==='login'){await api('/login',{method:'POST',body:Object.fromEntries(data)});await boot();}
  if(form.id==='password'){if(data.get('password')!==data.get('repeat'))throw Error('Пароли не совпадают.');const body={password:data.get('password')};if(data.get('currentPassword'))body.currentPassword=data.get('currentPassword');await api('/password',{method:'POST',body});await boot();message('Пароль сохранён. Остальные устройства вышли из аккаунта.');}
  if(form.id==='unlock'){await api(`/galleries/${gallery.id}/unlock`,{method:'POST',body:{password:data.get('password')}});gallery.locked=false;await openAlbum(gallery.id);}
  if(form.id==='createFilm'){const photoIds=data.getAll('photo').map(Number);if(photoIds.length<2||photoIds.length>12)throw Error('Выберите от 2 до 12 фотографий.');requestKey??=crypto.randomUUID();draft=await api('/films',{method:'POST',body:{requestKey,galleryId:gallery.id,photoIds,title:data.get('title'),names:data.get('names'),story:data.get('story'),format:data.get('format')}});renderDraft();await loadFilms();}
  if(form.id==='renderFilm'){const audio=form.elements.audio.files[0];if(audio){if(audio.size>12*1024*1024)throw Error('Музыка: до 12 МБ.');await api(`/films/${draft.id}/audio`,{method:'POST',raw:true,body:audio});}await api(`/films/${draft.id}/render`,{method:'POST',body:{lyrics:data.get('lyrics'),confirmed:data.get('confirmed')==='on'}});activeFilmId=draft.id;draft=null;$('#editor').innerHTML='';message('Ролик в очереди. Его статус сохранён: можно закрыть приложение и вернуться позже.');await loadFilms();}
 }catch(e){message(e.message);}finally{button.disabled=false;}
});
document.addEventListener('click',async e=>{
 const button=e.target.closest('button');if(!button)return;
 try{if(button.dataset.album)await openAlbum(button.dataset.album);
  if(button.dataset.action==='refresh')await boot();
  if(button.dataset.action==='logout'){await api('/logout',{method:'POST',body:{}});await loginView();}
  if(button.dataset.draft){const data=await api('/films');draft=data.films.find(f=>f.id===Number(button.dataset.draft));if(draft)renderDraft();}
  if(button.dataset.cancel){button.disabled=true;await api(`/films/${button.dataset.cancel}/cancel`,{method:'POST',body:{}});if(draft?.id===Number(button.dataset.cancel)){draft=null;$('#editor').innerHTML='';}await loadFilms();}
 }catch(e){message(e.message);}finally{button.disabled=false;}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(pollTimer);else if(me)loadFilms();});
boot();
