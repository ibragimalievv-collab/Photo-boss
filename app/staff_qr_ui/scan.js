const TOKEN=/^v1\.[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{43}$/;
const OWNED_ORIGINS=new Set(['https://photo-boss.ru','https://217.198.13.9:18443','https://photo-boss.onrender.com']);
const MAX_FILE_BYTES=20*1024*1024,MAX_FILE_DIMENSION=1600,MAX_CAMERA_DIMENSION=960;
const FRAME_INTERVAL=200;

export function qrDestination(raw,currentOrigin=location.origin){
 if(typeof raw!=='string'||raw.length>2048||raw!==raw.trim())return null;
 try{
  const here=new URL(currentOrigin),url=new URL(raw);
  const allowed=new Set([here.origin]);
  if(OWNED_ORIGINS.has(here.origin))for(const origin of OWNED_ORIGINS)allowed.add(origin);
  if(here.protocol!=='https:'||url.protocol!=='https:'||!allowed.has(url.origin)||url.username||url.password||/^https:\/\/[^/?#]*@/i.test(raw)||url.pathname!=='/staff-login/'||url.search)return null;
  const match=/^#token=(v1\.[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{43})$/.exec(url.hash);
  if(!match||!TOKEN.test(match[1]))return null;
  return here.origin+'/staff-login/#token='+match[1];
 }catch{return null;}
}

let decoderPromise=null,dialog=null,video=null,canvas=null,fileInput=null,message=null;
let stream=null,frame=null,runId=0,lastFrame=0,previousFocus=null,mode=null;
let activeImage=null,pendingImageCancel=null,fileUrl=null;

if(!document.querySelector('#pbQrScannerStyle')){
 const style=document.createElement('link');style.id='pbQrScannerStyle';style.rel='stylesheet';style.href='/staff-login/scan.css';document.head.append(style);
}

function status(text){if(message&&message.textContent!==text)message.textContent=text;}
function stopTracks(value){for(const track of value?.getTracks?.()||[])try{track.stop();}catch{}}
function releaseResources(){
 if(frame!==null){cancelAnimationFrame(frame);frame=null;}
 stopTracks(stream);stream=null;
 if(video){try{video.pause();}catch{}video.srcObject=null;video.hidden=true;}
 const cancel=pendingImageCancel;pendingImageCancel=null;cancel?.();
 if(activeImage){activeImage.src='';activeImage=null;}
 if(fileUrl){URL.revokeObjectURL(fileUrl);fileUrl=null;}
 if(canvas){canvas.width=1;canvas.height=1;}
}
function stop(){runId++;mode=null;releaseResources();}
function close(){stop();if(dialog?.open)dialog.close();if(fileInput)fileInput.value='';previousFocus?.focus?.();previousFocus=null;}
function current(ticket){return ticket===runId&&dialog?.open;}

function ensureDialog(){
 if(dialog)return;
 dialog=document.createElement('dialog');dialog.className='pb-qr-scan-dialog';dialog.setAttribute('aria-labelledby','pbQrScanTitle');
 dialog.innerHTML='<header class="pb-qr-scan-head"><h2 id="pbQrScanTitle">Вход по QR-коду</h2><button class="pb-qr-scan-close" type="button" data-qr-reader="close" aria-label="Закрыть сканирование">×</button></header><div class="pb-qr-scan-body"><video class="pb-qr-scan-video" autoplay muted playsinline hidden></video><canvas hidden></canvas><p class="pb-qr-scan-message" role="status" aria-live="polite"></p><div class="pb-qr-scan-actions"><button class="pb-qr-scan-button primary" type="button" data-qr-reader="camera">Открыть камеру</button><button class="pb-qr-scan-button" type="button" data-qr-reader="file">Выбрать QR из файла</button><button class="pb-qr-scan-button" type="button" data-qr-reader="close">Отмена</button></div><input type="file" accept="image/*" hidden aria-label="Изображение QR-кода"><p class="pb-qr-scan-note">Изображение распознаётся на вашем устройстве и не загружается на сервер.</p></div>';
 document.body.append(dialog);video=dialog.querySelector('video');video.muted=true;video.playsInline=true;canvas=dialog.querySelector('canvas');fileInput=dialog.querySelector('input');message=dialog.querySelector('[role="status"]');
 dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
 dialog.addEventListener('close',stop);
 dialog.addEventListener('click',event=>{const action=event.target.closest('[data-qr-reader]')?.dataset.qrReader;if(action==='close')close();else if(action==='file')chooseFile();else if(action==='camera')startCamera();});
 fileInput.addEventListener('change',()=>{const file=fileInput.files?.[0];fileInput.value='';if(file)readFile(file);});
 fileInput.addEventListener('cancel',()=>{if(dialog.open)status('Выберите изображение QR-кода или откройте камеру.');});
}
function show(){ensureDialog();if(!dialog.open){previousFocus=document.activeElement;dialog.showModal();}}

function loadDecoder(){
 if(typeof window.jsQR==='function')return Promise.resolve(window.jsQR);
 if(decoderPromise)return decoderPromise;
 decoderPromise=new Promise((resolve,reject)=>{
  const script=document.createElement('script');script.src='/staff-login/decoder.js';script.async=true;
  let done=false;const finish=ok=>{if(done)return;done=true;clearTimeout(timer);script.onload=script.onerror=null;if(ok&&typeof window.jsQR==='function')resolve(window.jsQR);else{decoderPromise=null;script.remove();reject(new Error('decoder_unavailable'));}};
  const timer=setTimeout(()=>finish(false),12000);script.onload=()=>finish(true);script.onerror=()=>finish(false);document.head.append(script);
 });
 return decoderPromise;
}

function navigate(raw){
 const target=qrDestination(raw);
 if(!target){status('Это не персональный QR для входа в Фотобосс. Используйте QR, который прислал владелец или администратор.');return false;}
 close();location.replace(target);return true;
}
function pixels(source,width,height,maximum){
 if(!width||!height||!Number.isFinite(width)||!Number.isFinite(height))return null;
 const scale=Math.min(1,maximum/Math.max(width,height));
 canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
 const context=canvas.getContext('2d',{willReadFrequently:true});
 if(!context)throw new Error('canvas_unavailable');
 context.drawImage(source,0,0,canvas.width,canvas.height);
 return context.getImageData(0,0,canvas.width,canvas.height);
}

async function startCamera(){
 show();stop();mode='camera';const ticket=runId;lastFrame=0;
 status('Готовим сканирование…');
 if(!navigator.mediaDevices?.getUserMedia){status('В этом браузере камера недоступна. Выберите QR из файла.');return;}
 try{
  const decode=await loadDecoder();if(!current(ticket))return;
  status('Разрешите доступ к камере, затем наведите её на QR сотрудника.');
  const camera=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:1280},height:{ideal:720}}});
  if(!current(ticket)){stopTracks(camera);return;}
  stream=camera;video.srcObject=stream;video.hidden=false;await video.play();
  if(!current(ticket))return;
  status('Наведите камеру на персональный QR-код сотрудника.');
  const scan=timestamp=>{
   if(!current(ticket))return;
   if(timestamp-lastFrame>=FRAME_INTERVAL&&video.readyState>=2){
    lastFrame=timestamp;
    try{const image=pixels(video,video.videoWidth,video.videoHeight,MAX_CAMERA_DIMENSION);if(image){const result=decode(image.data,image.width,image.height,{inversionAttempts:'attemptBoth'});if(result?.data&&navigate(result.data))return;}}
    catch{releaseResources();status('Не удалось считать изображение с камеры. Выберите QR из файла.');return;}
   }
   frame=requestAnimationFrame(scan);
  };
  frame=requestAnimationFrame(scan);
 }catch(error){
  if(!current(ticket))return;
  releaseResources();
  status(error?.message==='decoder_unavailable'?'Не удалось подготовить распознавание QR. Проверьте соединение и попробуйте ещё раз.':['NotAllowedError','PermissionDeniedError','SecurityError'].includes(error?.name)?'Доступ к камере не разрешён. Выберите QR из файла.':'Не удалось открыть камеру. Выберите QR из файла.');
 }
}

function chooseFile(){show();stop();mode='file';status('Выберите фото или скриншот с персональным QR-кодом.');fileInput.value='';fileInput.click();}
function loadImage(file){
 return new Promise((resolve,reject)=>{
  const image=new Image();activeImage=image;
  const url=URL.createObjectURL(file);fileUrl=url;let finished=false;
  const finish=error=>{
   if(finished)return;finished=true;image.onload=image.onerror=null;
   if(pendingImageCancel===cancel)pendingImageCancel=null;
   URL.revokeObjectURL(url);if(fileUrl===url)fileUrl=null;
   if(error)reject(error);else resolve(image);
  };
  const cancel=()=>{image.src='';finish(new Error('cancelled'));};pendingImageCancel=cancel;
  image.onload=()=>finish();image.onerror=()=>finish(new Error('image_unreadable'));image.src=url;
 });
}
async function readFile(file){
 show();stop();mode='file';const ticket=runId;
 if(!file.size||file.size>MAX_FILE_BYTES){status('Выберите изображение QR размером не больше 20 МБ.');return;}
 if(file.type==='image/svg+xml'||/\.svgz?$/i.test(file.name||'')||!(file.type?.startsWith('image/')||/\.(png|jpe?g|webp|gif|bmp|avif|heic|heif)$/i.test(file.name||''))){status('Выберите фото или скриншот QR-кода в формате изображения.');return;}
 status('Распознаём QR на этом устройстве…');
 try{
  const [decode,image]=await Promise.all([loadDecoder(),loadImage(file)]);
  if(!current(ticket))return;
  const width=image.naturalWidth,height=image.naturalHeight;
  if(!width||!height||width*height>80000000)throw new Error('image_too_large');
  const picture=pixels(image,width,height,MAX_FILE_DIMENSION);
  const result=picture?decode(picture.data,picture.width,picture.height,{inversionAttempts:'attemptBoth'}):null;
  if(result?.data){navigate(result.data);return;}
  status('На изображении не найден QR. Выберите более чёткое фото или скриншот, где код виден целиком.');
 }catch(error){
  if(!current(ticket)||error?.message==='cancelled')return;
  status(error?.message==='decoder_unavailable'?'Не удалось подготовить распознавание QR. Проверьте соединение и повторите выбор файла.':error?.message==='image_too_large'?'Изображение слишком большое. Выберите скриншот или уменьшенную копию с QR-кодом.':'Не удалось прочитать изображение. Выберите другое фото или скриншот QR.');
 }finally{if(current(ticket))releaseResources();}
}

document.addEventListener('click',event=>{
 const button=event.target.closest('[data-qr-scan]');if(!button)return;
 event.preventDefault();if(button.dataset.qrScan==='camera')startCamera();else if(button.dataset.qrScan==='file')chooseFile();
});
window.addEventListener('pagehide',close);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden'&&mode==='camera')close();});
document.addEventListener('pb-chat-account-reset',close);
