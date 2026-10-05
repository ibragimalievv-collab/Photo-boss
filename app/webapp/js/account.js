import {esc} from './domain.js';
import {preferBrowserSession} from './api.js';

const field=(label,input)=>`<label class="field"><span>${label}</span>${input}</label>`;
export function loginView(message=''){
 return `<div class="loading-screen"><h1>Вход в Photo Boss</h1><p>Войди под своим аккаунтом.</p><form id="passwordLoginForm" class="form-grid">${field('Логин','<input class="input" name="login" autocomplete="username" minlength="3" maxlength="50" autocapitalize="none" spellcheck="false" required>')}${field('Пароль','<input class="input" type="password" name="password" autocomplete="current-password" minlength="10" maxlength="128" required>')}<p id="formError" class="form-error" ${message?'':'hidden'}>${esc(message)}</p><button class="btn primary" type="submit">Войти</button></form><p class="small">Первый вход: подтверди аккаунт через Telegram один раз и создай личный пароль.</p><button class="btn ghost" data-action="browser-login">Первый вход через Telegram</button><button class="btn ghost" data-action="browser-password">Забыл пароль · восстановить через Telegram</button></div>`;
}

export function passwordForm(me){
 const configured=me.passwordConfigured;
 return `<p>${configured?'Для смены пароля введи текущий пароль или заново подтверди аккаунт через Telegram.':'Аккаунт подтверждён. Создай личный пароль для входа на компьютере и телефоне.'}</p><form id="passwordSetupForm" class="form-grid">${field('Логин',`<input class="input" name="login" value="${esc(me.passwordLogin||String(me.user.telegramId))}" autocomplete="username" minlength="3" maxlength="50" autocapitalize="none" spellcheck="false" required>`)}${configured?field('Текущий пароль','<input class="input" type="password" name="currentPassword" autocomplete="current-password" maxlength="128">'):''}${field('Новый пароль','<input class="input" type="password" name="password" autocomplete="new-password" minlength="10" maxlength="128" required>')}${field('Повтори новый пароль','<input class="input" type="password" name="repeatPassword" autocomplete="new-password" minlength="10" maxlength="128" required>')}<p class="small">От 10 символов. Логин запомни: он понадобится для входа.</p><p id="formError" class="form-error" hidden></p><button class="btn primary" type="submit">Сохранить пароль</button></form><button class="btn ghost" data-action="browser-password">Подтвердить аккаунт через Telegram</button>`;
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
