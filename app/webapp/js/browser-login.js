import {esc} from './domain.js';

let timer=null,generation=0,active=null;
export function stopBrowserLogin(){generation++;clearTimeout(timer);timer=null;active=null;}

async function call(name){
 const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);
 try{
  const response=await fetch(`/auth/browser/${name}`,{method:'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:{'X-PhotoBoss-Session':'1'}});
  const data=await response.json();
  if(!response.ok){const error=new Error(data.error||'Не удалось выполнить вход.');error.status=response.status;throw error;}
  return data;
 }catch(error){if(error.name==='AbortError')throw new Error('Сервер отвечает дольше обычного. Повторите попытку.');throw error;}finally{clearTimeout(timeout);}
}

export async function startBrowserLogin(onLogin){
 stopBrowserLogin();
 const ticket=generation,data=await call('start');
 if(ticket!==generation)return;
 const link=new URL(data.telegramUrl);
 if(link.origin!=='https://t.me'||!/^[A-Za-z0-9_]+$/.test(link.pathname.slice(1))||!/^\d{6}$/.test(data.code))throw new Error('Сервер вернул некорректные данные входа.');
 active={ticket,onLogin,expiresAt:data.expiresAt};
 document.querySelector('#app').innerHTML=`<div class="loading-screen"><h1>Вход в Photo Boss</h1><p>Подтверди вход в Telegram. Код в боте должен совпасть с кодом на этом экране.</p><p><strong>${esc(data.code)}</strong></p><a class="btn primary" href="${esc(link.href)}" target="_blank" rel="noopener noreferrer">Открыть Telegram</a><p>После подтверждения вернись в эту вкладку.</p><p id="browserLoginStatus" role="status">Ожидаем подтверждение…</p><button class="btn ghost" data-action="browser-login-check">Проверить вход</button><button class="btn ghost" data-action="browser-login">Начать заново</button></div>`;
 schedule();
}

function schedule(){clearTimeout(timer);if(active)timer=setTimeout(()=>checkBrowserLogin(),2500);}

export async function checkBrowserLogin(){
 const current=active;if(!current||current.checking)return;
 if(Date.now()/1000>=current.expiresAt){stopBrowserLogin();const status=document.querySelector('#browserLoginStatus');if(status)status.textContent='Время подтверждения истекло. Начни вход заново.';return;}
 current.checking=true;
 try{
  const data=await call('poll');if(active!==current)return;
  if(data.status==='authenticated'){const onLogin=current.onLogin;stopBrowserLogin();await onLogin();return;}
  const status=document.querySelector('#browserLoginStatus');if(status)status.textContent='Ожидаем подтверждение в Telegram…';
 }catch(error){
  if(active!==current)return;
  const status=document.querySelector('#browserLoginStatus');if(status)status.textContent=error.message||'Нет соединения. Проверяем вход повторно…';
  if(error.status&&error.status<500){stopBrowserLogin();return;}
 }finally{current.checking=false;if(active===current)schedule();}
}

document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkBrowserLogin();});
window.addEventListener('focus',()=>checkBrowserLogin());
