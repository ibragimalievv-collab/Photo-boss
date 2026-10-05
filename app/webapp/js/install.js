// Installation is public; working data still require the existing Telegram login.
const bar=document.querySelector('#installBar');
const button=document.querySelector('#installApp');
const dialog=document.querySelector('#installDialog');
const status=document.querySelector('#installStatus');
const display=window.matchMedia('(display-mode: standalone)');
const ios=/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const android=/Android/.test(navigator.userAgent);
const installed=()=>display.matches||navigator.standalone===true;
let deferredPrompt=null,installing=false,installationCompleted=false;

function refresh(){bar.hidden=installed()||installationCompleted;}
function showInstructions(){
 status.textContent='';
 document.querySelector('#installEmbedded').hidden=!window.Telegram?.WebApp?.initData&&!/Telegram|; wv\)/i.test(navigator.userAgent);
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
const link=new URL('/app/',window.location.origin).href;
document.querySelector('#installLink').value=link;
document.querySelector('#openInstallLink').href=link;
document.querySelector('#copyInstallLink').addEventListener('click',async()=>{
 try{await navigator.clipboard.writeText(link);status.textContent='Ссылка скопирована. Её можно отправить сотрудникам.';}
 catch{document.querySelector('#installLink').select();status.textContent='Скопируй выделенную ссылку.';}
});
refresh();
