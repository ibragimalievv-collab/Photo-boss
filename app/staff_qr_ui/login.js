// The credential is sent only in same-origin POST bodies and is never persisted.
let credential='',closed=false,requestController=null;
try{
 const fragment=new URLSearchParams(location.hash.slice(1));
 const values=fragment.getAll('token');
 if(values.length===1&&[...fragment.keys()].length===1&&/^[A-Za-z0-9._-]{20,512}$/.test(values[0]))credential=values[0];
 history.replaceState(null,'','/staff-login/');
}catch{credential='';}

const status=document.querySelector('#loginStatus');
const identity=document.querySelector('#loginIdentity');
const name=document.querySelector('#loginName');
const roles=document.querySelector('#loginRoles');
const problem=document.querySelector('#loginError');
const button=document.querySelector('#loginButton');
const retry=document.querySelector('#retryButton');
const help=document.querySelector('#loginHelp');
const roleNames={OWNER:'Владелец',ADMIN:'Администратор',MANAGER:'Менеджер по записи',PHOTOGRAPHER:'Фотограф'};
let employee=null,busy=false;

function failed(message,canRetry=false){
 status.textContent='Не удалось войти по QR-коду.';
 problem.textContent=message;problem.hidden=false;
 identity.hidden=true;button.hidden=true;retry.hidden=!canRetry;
 help.textContent=canRetry?'Проверьте соединение и повторите попытку.':'Попросите владельца или администратора открыть ваш персональный QR и отправить его заново.';
}

async function post(path){
 const controller=new AbortController();requestController=controller;
 const timer=setTimeout(()=>controller.abort(),15000);
 try{
  const response=await fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',
   signal:controller.signal,headers:{'Content-Type':'application/json','X-PhotoBoss-Session':'1'},
   body:JSON.stringify({token:credential})});
  const data=await response.json().catch(()=>null);
  if(!response.ok){
   const error=new Error(typeof data?.error==='string'?data.error.slice(0,300):'Код недоступен. Попросите отправить новый QR.');
   error.retryable=response.status>=500||response.status===429;throw error;
  }
  if(!data||typeof data!=='object')throw new Error('Не удалось проверить ответ сервера. Повторите вход по QR.');
  return data;
 }catch(error){
  if(error.retryable!==undefined)throw error;
  const safe=new Error('Нет ответа от Фотобосса. Проверьте соединение и повторите попытку.');safe.retryable=true;throw safe;
 }finally{clearTimeout(timer);if(requestController===controller)requestController=null;}
}

async function inspect(){
 if(busy||closed)return;
 if(!credential)return failed('В этой ссылке нет действующего QR для входа. Откройте QR, который прислал администратор.');
 busy=true;employee=null;problem.hidden=true;retry.hidden=true;button.hidden=true;identity.hidden=true;
 status.textContent='Проверяем код сотрудника…';
 try{
  const data=await post('/auth/qr/inspect');
  if(closed)return;
  if(!data.employee||typeof data.employee.name!=='string'||!data.employee.name.trim()||!Array.isArray(data.employee.roles))throw new Error('Карточка входа недоступна. Попросите новый QR.');
  employee=data.employee;
  name.textContent=employee.name;roles.textContent=employee.roles.map(role=>roleNames[role]).filter(Boolean).join(' · ');
  status.textContent='Проверьте, что это ваш аккаунт.';
  button.textContent=`Войти как ${employee.name}`;
  identity.hidden=false;button.hidden=false;button.disabled=false;
  help.textContent='Нажмите кнопку — Фотобосс откроется с вашими правами. Логин и пароль вводить не нужно.';
 }catch(error){if(!closed)failed(error.message,error.retryable===true);}
 finally{busy=false;}
}

button.addEventListener('click',async()=>{
 if(busy||closed||!credential||!employee)return;
 busy=true;button.disabled=true;problem.hidden=true;retry.hidden=true;
 status.textContent='Выполняем вход…';
 try{
  const data=await post('/auth/qr/login');
  if(closed)return;
  if(data.status!=='authenticated')throw new Error('Сеанс не подтверждён. Откройте QR заново.');
  credential='';status.textContent='Вход выполнен. Открываем Фотобосс…';
  // No credential is stored: this flag only selects the existing HttpOnly cookie.
  try{sessionStorage.setItem('pb_browser_session_only','1');sessionStorage.removeItem('pb_init_data_v1');sessionStorage.removeItem('pb_owner_launch_v1');}catch{}
  try{const {accountChanged}=await import('/app/js/account.js');if(!closed)accountChanged();}
  catch{if(!closed)location.replace('/app/#home');}
 }catch(error){
  if(!closed){problem.textContent=error.message;problem.hidden=false;status.textContent='Вход не завершён. Попробуйте ещё раз.';button.disabled=false;}
 }finally{busy=false;}
});
retry.addEventListener('click',inspect);
window.addEventListener('pagehide',()=>{closed=true;credential='';employee=null;requestController?.abort();button.disabled=true;});
window.addEventListener('pageshow',event=>{if(event.persisted){closed=false;employee=null;credential='';failed('Откройте персональный QR заново для входа.');}});
inspect();
