import {esc} from './domain.js';
import {preferBrowserSession} from './api.js';
// The QR scanner only serves the login screen. Loaded on the side, so a file
// that fails to load disables the scan button instead of the whole app.
import('/staff-login/scan.js').catch(()=>{});

const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
const standaloneMode=()=>window.PHOTO_BOSS_CONFIG?.STANDALONE===true;
export function loginView(message=''){
 const recovery=standaloneMode()?'<p class="small">Для первого входа попроси у владельца или администратора персональный QR. Если пароль забыт, попроси отправить твой QR ещё раз для восстановления доступа.</p>':'<p class="small">Первый вход: подтверди аккаунт через Telegram один раз и создай личный пароль.</p><button class="btn ghost" data-action="browser-login">Первый вход через Telegram</button><button class="btn ghost" data-action="browser-password">Забыл пароль · восстановить через Telegram</button>';
 return `<div class="loading-screen"><h1>Вход в Photo Boss</h1><p>Войди под своим аккаунтом.</p><section class="pb-login-qr-options" aria-label="Вход сотрудника по QR"><button class="btn primary" type="button" data-qr-scan="camera">Сканировать QR-код</button><button class="btn ghost" type="button" data-qr-scan="file">Выбрать QR из файла</button><p class="small">QR от владельца или администратора заменяет логин и пароль.</p></section><form id="passwordLoginForm" class="form-grid">${field('Логин','<input class="input" name="login" autocomplete="username" minlength="3" maxlength="50" autocapitalize="none" spellcheck="false" required>')}${field('Пароль','<input class="input" type="password" name="password" autocomplete="current-password" minlength="10" maxlength="128" required>')}<p id="formError" class="form-error" ${message?'':'hidden'}>${esc(message)}</p><button class="btn primary" type="submit">Войти</button></form>${recovery}</div>`;
}

export function passwordForm(me){
 const configured=me.passwordConfigured;
 const native=standaloneMode();
 const explanation=native?(configured?'Для смены пароля введи текущий пароль. Если он забыт, повторно войди по персональному QR от владельца или администратора.':'После свежего входа по персональному QR можно создать личный пароль для компьютера и телефона.'):(configured?'Для смены пароля введи текущий пароль или заново подтверди аккаунт через Telegram.':'Аккаунт подтверждён. Создай личный пароль для входа на компьютере и телефоне.');
 const login=me.passwordLogin||(native?`staff-${me.user.id}`:String(me.user.telegramId));
 return `<p>${explanation}</p><form id="passwordSetupForm" class="form-grid">${field('Логин',`<input class="input" name="login" value="${esc(login)}" autocomplete="username" minlength="3" maxlength="50" autocapitalize="none" spellcheck="false" required>`)}${configured?field('Текущий пароль','<input class="input" type="password" name="currentPassword" autocomplete="current-password" maxlength="128">'):''}${field('Новый пароль','<input class="input" type="password" name="password" autocomplete="new-password" minlength="10" maxlength="128" required>')}${field('Повтори новый пароль','<input class="input" type="password" name="repeatPassword" autocomplete="new-password" minlength="10" maxlength="128" required>')}<p class="small">От 10 символов. Логин запомни: он понадобится для входа.</p><p id="formError" class="form-error" hidden></p><button class="btn primary" type="submit">Сохранить пароль</button></form>${native?'':'<button class="btn ghost" data-action="browser-password">Подтвердить аккаунт через Telegram</button>'}`;
}

export async function accountCall(name,body={}){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try{
  const response=await fetch(`/auth/password/${name}`,{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal,headers:{'Content-Type':'application/json','X-PhotoBoss-Session':'1'},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error||'Не удалось выполнить вход.');error.status=response.status;throw error;}
  return data;
 }catch(error){if(error.name==='AbortError')throw new Error('Сервер отвечает дольше обычного. Повторите попытку.');throw error;}finally{clearTimeout(timer);}
}

export function accountChanged(){
 preferBrowserSession();
 try{localStorage.setItem('pb-account-changed',crypto.randomUUID());}catch{}
 location.replace(`/app/?account=${Date.now()}#home`);
}
window.addEventListener('storage',event=>{
 if(event.key==='pb-account-changed'){preferBrowserSession();location.replace(`/app/?account=${Date.now()}#home`);}
});
