import {api} from './api.js';
import {esc} from './domain.js';
const count=value=>Number.isSafeInteger(value)?String(value):'Не проверено';
export async function salesStorageView(){
 const data=await api('/storage/sales-audit',{allowSnapshot:false});
 return `<p>Сверка выполнена ${esc(new Date(data.checkedAt).toLocaleString('ru-RU'))}. Выбранные кадры и полная съёмка считаются отдельно. Копии в папках выдачи учитываются один раз по контрольной сумме.</p>${data.items.map(item=>{
  const chosen=item.selectedFolder,full=item.fullShootFolders;
  return `<article class="panel section"><h3>Запись №${item.bookingId} · ${item.draftStatus==='CANCELLED'?'Продажа обнулена':'Оформление №'+item.draftId}</h3><p>Указано: вся съёмка ${count(item.declaredTotal)}, продано ${count(item.declaredSold)}.</p><p>Выбранные: файлов на Диске ${count(chosen.diskFiles)}, уникальных ${count(chosen.verifiedUniqueFiles)}, учтено в базе ${count(chosen.registeredFiles)}, подтверждено ${count(chosen.registeredVerified)}.</p><p>Не хватает выбранных: ${count(chosen.missingSelected)}. Точных копий: ${count(chosen.exactDuplicateFiles)}. Файлов вне учёта: ${count(chosen.unregisteredFiles)}.</p><p>Полная съёмка: файлов ${count(full.diskFiles)}, уникальных ${count(full.uniqueFiles)}, точных копий ${count(full.exactDuplicateFiles)}, не хватает оригиналов ${count(full.missingOriginals)}.</p>${chosen.status==='unavailable'||full.status==='unavailable'?'<p role="status">Часть папок сейчас недоступна. Количество не подтверждено; повторите сверку.</p>':''}</article>`;
 }).join('')||'<p>Продаж для сверки пока нет.</p>'}${data.limited?'<p>Показаны последние 30 оформлений.</p>':''}<p>Похожие ракурсы, качество и композиция — в разделе «AI-развитие и продажи». Это рекомендации по фотографии; они не начисляют штрафы и не удаляют исходники.</p><a class="btn ghost" href="#development">Открыть разбор съёмок</a>`;
}
