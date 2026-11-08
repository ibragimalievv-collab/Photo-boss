export function guestPhone(value){
 const raw=String(value??'').trim();if(!raw)return null;
 if(raw.length>80||!/^\+?[0-9\s()\-]+$/.test(raw))throw Error('Укажите телефон цифрами с кодом страны.');
 const digits=raw.replace(/\D/g,'');
 if(raw.startsWith('+')){
  if(digits.length<7||digits.length>15||digits.startsWith('0')||(digits.startsWith('7')&&digits.length!==11))throw Error('Проверьте длину номера и код страны.');
  return '+'+digits;
 }
 if(digits.length===10)return '+7'+digits;
 if(digits.length===11&&'78'.includes(digits[0]))return '+7'+digits.slice(1);
 throw Error('Укажите 10 цифр российского номера, +7 или международный код с +.');
}

export function phoneMatches(value,bookings){
 const phone=guestPhone(value);if(!phone)return 0;
 return bookings.filter(row=>{try{return guestPhone(row.phone)===phone;}catch{return false;}}).length;
}
