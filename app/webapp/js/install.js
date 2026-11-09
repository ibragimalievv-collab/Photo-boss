// Installation is public; the server still authenticates every working account.
const standaloneMode=()=>window.PHOTO_BOSS_CONFIG?.STANDALONE===true;
const bar=document.querySelector('#installBar');
const button=document.querySelector('#installApp');
const dialog=document.querySelector('#installDialog');
const status=document.querySelector('#installStatus');
const display=window.matchMedia('(display-mode: standalone)');
const ios=/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const android=/Android/.test(navigator.userAgent);
const installed=()=>display.matches||navigator.standalone===true;
// The previous address (photo-boss.onrender.com: abroad, behind Cloudflare) needs a VPN
// in Russia; photo-boss.ru is hosted in Russia and does not. Send everyone there.
const CANONICAL_ORIGIN='https://photo-boss.ru';
const legacyAddress=/(^|\.)onrender\.com$/i.test(new URL(window.location.origin).hostname);
let newAddress=document.querySelector('#newAddressBar');
if(!newAddress&&legacyAddress&&typeof document.createElement==='function'){
 // A page shell cached before this notice existed: draw it here.
 newAddress=document.createElement('div');newAddress.id='newAddressBar';newAddress.className='new-address-bar';newAddress.setAttribute('role','alert');
 const text=document.createElement('p');text.textContent='Photo Boss переехал на photo-boss.ru — там вход работает без VPN. Откройте новый адрес, войдите по личному QR или логину и паролю и установите приложение заново.';
 const link=document.createElement('a');link.className='btn primary';link.href=CANONICAL_ORIGIN+'/app/';link.rel='noopener';link.textContent='Открыть photo-boss.ru';
 newAddress.append(text,link);
 const workspace=document.querySelector('.workspace');
 if(workspace)workspace.prepend(newAddress);else document.body.prepend(newAddress);
}
if(newAddress)newAddress.hidden=!legacyAddress;
let deferredPrompt=null,installing=false,installationCompleted=false;
if(standaloneMode()){
 const hint=document.querySelector('#installLoginHint');
 if(hint)hint.textContent='После установки открой Photo Boss и войди по персональному QR или логину и паролю. QR для первого входа и восстановления доступа выдаёт владелец или администратор.';
 document.querySelector('#installEmbedded').textContent='Открой эту ссылку в обычном браузере: Chrome на Android или Safari на iPhone.';
}

function refresh(){bar.hidden=installed()||installationCompleted;}
function showInstructions(){
 status.textContent='';
 document.querySelector('#installEmbedded').hidden=standaloneMode()?!/Telegram|; wv\)/i.test(navigator.userAgent):!window.Telegram?.WebApp?.initData&&!/Telegram|; wv\)/i.test(navigator.userAgent);
 document.querySelector('#installInstructions').innerHTML=ios
  ? '<h3>iPhone и iPad</h3><ol><li>Открой ссылку в Safari.</li><li>Нажми «Поделиться» и выбери «На экран Домой».</li><li>Если есть переключатель «Открывать как веб-приложение», включи его. Нажми «Добавить».</li></ol>'
  : android
   ? '<h3>Android</h3><ol><li>Открой ссылку в Chrome.</li><li>В меню ⋮ выбери «Установить приложение» или «Добавить на главный экран».</li><li>Подтверди установку.</li></ol>'
   : '<h3>Компьютер</h3><ol><li>Открой ссылку в Chrome или Edge.</li><li>Нажми значок установки в адресной строке или выбери установку приложения в меню браузера.</li><li>На Mac в Safari выбери «Файл» → «Добавить в Dock».</li></ol><p class="small">На телефоне: Android — меню Chrome → установка; iPhone — Safari → «Поделиться» → «На экран Домой».</p>';
 if(!dialog.open)dialog.showModal();
}

window.addEventListener('beforeinstallprompt',event=>{
 event.preventDefault();deferredPrompt=event;refresh();
});
window.addEventListener('appinstalled',()=>{
 installationCompleted=true;deferredPrompt=null;refresh();if(dialog.open)dialog.close();
});
display.addEventListener?.('change',refresh);
window.addEventListener('pageshow',refresh);
button.addEventListener('click',async()=>{
 if(installed()||installationCompleted||installing)return;
 if(!deferredPrompt){showInstructions();return;}
 const prompt=deferredPrompt;deferredPrompt=null;installing=true;button.disabled=true;
 try{
  await prompt.prompt();
  const choice=await prompt.userChoice;
  // Acceptance is not proof installation completed; appinstalled hides the button.
  if(choice.outcome!=='accepted')showInstructions();
 }catch{showInstructions();}
 finally{installing=false;button.disabled=false;refresh();}
});
document.querySelector('#closeInstall').addEventListener('click',()=>dialog.close());
const link=new URL('/app/',legacyAddress?CANONICAL_ORIGIN:window.location.origin).href;
document.querySelector('#installLink').value=link;
document.querySelector('#openInstallLink').href=link;
document.querySelector('#copyInstallLink').addEventListener('click',async()=>{
 try{await navigator.clipboard.writeText(link);status.textContent='Ссылка скопирована. Её можно отправить сотрудникам.';}
 catch{document.querySelector('#installLink').select();status.textContent='Скопируй выделенную ссылку.';}
});
refresh();
