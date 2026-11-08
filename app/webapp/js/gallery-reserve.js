/* Recovery upload shares the normal durable media queue; it creates no sale. */
export function reserveCandidates(projects){
 return projects.filter(p=>!p.trashed&&p.client?.trim()&&p.hotelId&&p.photographerId&&p.date&&p.time&&p.time!=='None');
}
export function reserveFiles(files){
 const photos=[...files].filter(f=>['image/jpeg','image/png','image/webp'].includes(f.type)||(!f.type&&/\.(jpe?g|png|webp)$/i.test(f.name)));
 if(!photos.length)throw Error('Выберите JPEG, PNG или WebP · до 20 МБ на один файл.');
 if(photos.some(f=>!f.size||f.size>20*1024*1024))throw Error('Есть пустые фотографии или отдельные файлы больше 20 МБ. Лимит относится к одному файлу, а не ко всей папке.');
 return photos;
}
export function reserveOperation(bookingId,file,date){
 if(!Number.isSafeInteger(bookingId)||bookingId<1||bookingId>=2**31)throw Error('Выберите назначенную съёмку.');
 return {kind:'gallery_reserve_photo',date,data:{booking:bookingId,filename:file.name}};
}
