import {api} from './api.js';
import {esc} from './domain.js';
let actor=null,path='disk:/',jobId=null,poll=null;
const stop=()=>{if(poll)clearTimeout(poll);poll=null;};
window.addEventListener('hashchange',stop);window.addEventListener('pagehide',stop);
export function diskFolderPanel(data){
 if(!data.canAuditDisk)return '';
 if(actor!==data.actorId){stop();actor=data.actorId;path='disk:/';jobId=null;try{jobId=sessionStorage.getItem('pb-disk-audit:'+actor)||null;}catch{}}
 if(jobId){stop();poll=setTimeout(()=>refresh().catch(showError),0);}
 return `<details class="panel section"><summary>Проверить папку Яндекс.Диска без загрузки фотографий</summary><section id="diskFolderAudit"><p>Выберите папку на подключённом рабочем Диске. Проверка включает вложенные папки, читает оригиналы и сохраняет отчёт. На одну проверку — до 1000 фотографий и 200 папок. Исходники и касса не изменяются.</p><label class="field">Связать отчёт с записью<select class="input" id="diskAuditBooking"><option value="">Общая проверка папки</option>${data.bookings.map(b=>`<option value="${b.id}">№${b.id} · ${esc(b.hotel)} · ${esc(b.client)} · ${esc(b.date)}</option>`).join('')}</select></label><button type="button" class="btn ghost" data-disk-open="disk:/">Открыть папки Яндекс.Диска</button><div id="diskAuditFolders"></div><p id="diskAuditMessage" role="status"></p><div id="diskAuditResult"></div></section></details>`;
}
async function folders(next){
 const result=await api('/storage/disk-folders?path='+encodeURIComponent(next),{allowSnapshot:false});path=result.path;
 const parent=path==='disk:/'?null:path.slice(0,path.lastIndexOf('/'))||'disk:';
 const target=document.querySelector('#diskAuditFolders');if(!target)return;
 target.innerHTML=`<p>Папка: <strong>${esc(path)}</strong> · файлов непосредственно в папке: ${result.filesHere}</p>${parent?`<button type="button" class="btn ghost" data-disk-open="${esc(parent==='disk:'?'disk:/':parent)}">На уровень выше</button>`:''}${result.folders.map(f=>`<button type="button" class="btn ghost" data-disk-open="${esc(f.path)}">📁 ${esc(f.name)}</button>`).join('')}<button type="button" class="btn primary" data-disk-scan="${esc(path)}">Проверить эту папку и вложенные</button>`;
}
export function renderDiskAudit(job){
 const s=job.summary,states={PENDING:'В очереди',RUNNING:job.stage==='LISTING'?'Читаем список файлов':'Проверяем фотографии',COMPLETED:'Обработка завершена',FAILED:'Проверка не завершена'};
 const names=new Map(job.files.map(f=>[f.photoId,f.name]));
 const pairs=list=>list.map(pair=>pair.map(id=>esc(names.get(id)||`Фото ${id}`)).join(' ↔ ')).map(text=>`<li>${text}</li>`).join('');
 return `<article class="notice"><h3>${esc(states[job.status]||job.status)}</h3><p>${esc(job.path)}${job.bookingId?` · запись №${job.bookingId}`:''}</p><p>${s.listingComplete?'Найдено фотографий':'Пока найдено фотографий'}: ${job.totalPhotos}. Обработано: ${job.processed}. Прочитано и проверено: ${s.checked}. Не удалось проверить: ${s.failed}. Прочих файлов: ${job.otherFiles}.</p><p>Уникальных среди прочитанных оригиналов: ${s.uniqueOriginals}. ${s.complete?'Техническая проверка всех найденных фотографий завершена.':'Полная техническая проверка пока не подтверждена.'}</p>${job.error?`<p role="alert">${esc(job.error)}</p>`:''}<details><summary>Точные копии · показано пар ${s.exactCopies.length}</summary><ul>${pairs(s.exactCopies)}</ul></details><details><summary>Похожие кадры · кандидатов ${s.similarCandidates.length}</summary><ul>${pairs(s.similarCandidates)}</ul><p>Похожий ракурс не означает брак. Оцените кадры вручную.</p></details>${job.files.filter(f=>f.status!=='checked').map(f=>`<p>${esc(f.name)}: ${esc(f.error)}</p>`).join('')}<p>AI-разбор: ${job.aiFrames.length} кадров.${job.aiConfigured?'':' AI сейчас не настроен; визуальное качество, позы и ракурсы не оценены.'}</p>${job.aiErrors.map(e=>`<p>${esc(e.error)} · кадров ${e.photos.length}</p>`).join('')}${job.aiFrames.map(f=>`<details><summary>${esc(f.name)} · рекомендации AI</summary><p>Техника: ${esc(f.technical)}</p><p>Композиция: ${esc(f.composition)}</p><p>Поза: ${esc(f.pose)}</p><p>Ракурс: ${esc(f.angle)}</p><p>Рекомендация: ${esc(f.recommendation)}</p><p>Неопределённость: ${esc(f.uncertainty)}</p></details>`).join('')}<p>${esc(job.note)}</p></article>`;
}
async function refresh(){
 if(!jobId||!document.querySelector('#diskFolderAudit'))return;
 const job=await api('/storage/disk-audits/'+jobId,{allowSnapshot:false});
 const target=document.querySelector('#diskAuditResult');if(!target)return;
 target.innerHTML=renderDiskAudit(job);
 if(['PENDING','RUNNING'].includes(job.status))poll=setTimeout(()=>refresh().catch(showError),3000);
}
function showError(error){const el=document.querySelector('#diskAuditMessage');if(el)el.textContent=error.message;}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-disk-open],[data-disk-scan]');if(!button)return;
 button.disabled=true;
 try{
  if(button.dataset.diskOpen){await folders(button.dataset.diskOpen);document.querySelector('#diskAuditMessage').textContent='Выберите папку, которую нужно проверить.';}
  else{
   stop();const booking=document.querySelector('#diskAuditBooking').value;
   const requestKey=button.dataset.requestKey||crypto.randomUUID();button.dataset.requestKey=requestKey;
   const job=await api('/storage/disk-audits',{method:'POST',body:{path:button.dataset.diskScan,bookingId:booking?Number(booking):null,requestKey}});
   jobId=job.id;try{sessionStorage.setItem('pb-disk-audit:'+actor,jobId);}catch{}delete button.dataset.requestKey;document.querySelector('#diskAuditMessage').textContent='Проверка зарегистрирована. Закрытие страницы не отменяет работу сервера.';await refresh();
  }
 }catch(error){showError(error);}finally{button.disabled=false;}
});
