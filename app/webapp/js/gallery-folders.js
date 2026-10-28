/* Staff folder navigation uses only projects already permitted by the server. */
const months=['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
export const galleryFolderLevels=['Отели','Месяцы','Числа','Фотографы','Съёмки гостей'];
export function galleryFolderPath(project){
 if(Array.isArray(project.folderPath)&&project.folderPath.length===4&&project.folderPath.every(p=>p&&typeof p.key==='string'&&typeof p.label==='string'))return project.folderPath;
 const match=/^(\d{4})-(\d{2})-(\d{2})$/.exec(project.date||''),month=match?Number(match[2]):0,day=match?Number(match[3]):0;
 const valid=match&&month>=1&&month<=12&&day>=1&&day<=31&&new Date(project.date+'T12:00:00Z').toISOString().slice(0,10)===project.date;
 return [
  {key:project.hotelId?`hotel:${project.hotelId}`:'hotel:unknown',label:project.hotel||'Отель не указан',sort:project.hotel||''},
  valid?{key:project.date.slice(0,7),label:`${months[month-1]} ${match[1]}`,sort:project.date.slice(0,7)}:{key:'month:unknown',label:'Месяц не указан',sort:''},
  valid?{key:project.date,label:String(day),sort:project.date}:{key:'day:unknown',label:'Число не указано',sort:''},
  {key:project.photographerId?`photographer:${project.photographerId}`:'photographer:unknown',label:project.photographer||'Фотограф не указан',sort:project.photographer||''}
 ];
}
export function galleryFolderProjects(projects,trail){
 return projects.filter(project=>trail.every((segment,index)=>galleryFolderPath(project)[index]?.key===segment.key));
}
export function galleryFolderGroups(projects,trail,ascending=false){
 if(trail.length>=4)return [];
 const groups=new Map();
 for(const project of galleryFolderProjects(projects,trail)){
  const segment=galleryFolderPath(project)[trail.length];
  if(!groups.has(segment.key))groups.set(segment.key,{...segment,shoots:0,photos:0,bytes:0});
  const group=groups.get(segment.key);group.shoots++;group.photos+=project.photoCount||0;group.bytes+=project.bytes||0;
 }
 return [...groups.values()].sort((a,b)=>{
  const missingA=a.key.endsWith(':unknown'),missingB=b.key.endsWith(':unknown');
  if(missingA!==missingB)return missingA?1:-1;
  return (ascending?1:-1)*(String(a.sort??a.label).localeCompare(String(b.sort??b.label),'ru',{numeric:true})||a.key.localeCompare(b.key));
 });
}
export function galleryProjectMatches(project,search){
 return [project.title,project.client,project.hotel,project.photographer,project.room,project.id,project.date,
  ...galleryFolderPath(project).map(p=>p.label)].join(' ').toLocaleLowerCase('ru').includes(search.trim().toLocaleLowerCase('ru'));
}
export function galleryProjectTitle(project){return project.client?.trim()||'Имя гостя не указано';}
export function galleryProjectDetails(project){
 return [`Съёмка №${project.id}`,project.time&&project.time!=='None'?project.time:null,
  project.room?`комната ${project.room}`:null,project.guestCount?`гостей: ${project.guestCount}`:null,
  project.title&&project.title!==project.client?project.title:null].filter(Boolean).join(' · ');
}
