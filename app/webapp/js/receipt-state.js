// Receipt admission and bank settlement are distinct facts throughout the UI.
export function receiptTitle(receipt){
 if(receipt.bankConfirmed===true)return receipt.status==='APPROVED'?'оплата подтверждена банком':'банк сообщил возврат';
 if(receipt.automatic?.state==='accepted'&&receipt.status==='APPROVED')return 'чек принят автоматически';
 if(receipt.status==='REJECTED')return 'чек не принят';
 return receipt.stored?'файл принят сервером':'проверка чека';
}
export function receiptMessage(receipt,{automatic=false}={}){
 if(receipt.bankConfirmed===true)return receipt.status==='APPROVED'?'Поступление подтверждено банком.':'Банк сообщил возврат. Сумма оплаты пересчитана.';
 const state=receipt.automatic;
 if(state?.state==='accepted'&&receipt.status==='APPROVED')return 'Чек принят автоматически и учтён в оплате. Отдельное подтверждение владельцем не требуется. Подтверждение банка пока не получено.';
 if(state?.messages?.length)return state.messages.join(' ');
 if(state||automatic)return receipt.status==='REJECTED'?'Прикрепите исправленный чек.':'Чек проверяется автоматически. Решение владельца не требуется.';
 return ({PENDING:'Оплата пока не подтверждена: владелец должен сверить поступление с банковской выпиской.',
          APPROVED:'Поступление подтверждено владельцем.',REJECTED:'Чек отклонён владельцем.'})[receipt.status]||'Подтверждение оплаты отсутствует.';
}
