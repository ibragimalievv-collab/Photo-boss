import assert from 'node:assert/strict';
import {galleryFolderPath,galleryFolderGroups,galleryFolderProjects,galleryProjectMatches,galleryProjectTitle,galleryProjectDetails} from '../app/webapp/js/gallery-folders.js';
const project=(id,change={})=>({id,title:'Семейная съёмка',hotelId:1,hotel:'Отель',photographerId:2,photographer:'Анна',client:'Алиса',date:'2026-10-02',time:'12:00',room:'201',guestCount:2,photoCount:3,bytes:100,...change});
const rows=[project(1),project(2,{photographerId:3,photographer:'Борис',client:'Боб'}),
 project(3,{date:'2026-11-10'}),project(4,{date:'2026-10-10'}),project(5,{hotelId:2,hotel:'Другой отель'}),
 project(6,{date:'2025-10-02'}),project(7,{client:'Алиса'}),project(8,{hotelId:null,hotel:'',photographerId:null,photographer:'',date:null})];
const hotel=galleryFolderPath(rows[0])[0],october=galleryFolderPath(rows[0])[1],day=galleryFolderPath(rows[0])[2],anna=galleryFolderPath(rows[0])[3];
assert.deepEqual(galleryFolderGroups(rows,[hotel],true).map(x=>x.label),['Октябрь 2025','Октябрь 2026','Ноябрь 2026']);
assert.deepEqual(galleryFolderGroups(rows,[hotel,october],true).map(x=>x.label),['2','10']);
assert.deepEqual(galleryFolderGroups(rows,[hotel,october,day],true).map(x=>x.label),['Анна','Борис']);
assert.deepEqual(galleryFolderProjects(rows,[hotel,october,day,anna]).map(x=>x.id),[1,7]);
assert.equal(galleryFolderGroups(rows,[],true).at(-1).label,'Отель не указан');
assert.equal(galleryFolderPath(rows.at(-1))[3].label,'Фотограф не указан');
assert.equal(galleryFolderPath(project(10,{date:'2026-02-30'}))[1].label,'Месяц не указан');
assert.ok(galleryProjectMatches(rows[2],'Ноябрь 2026'));assert.ok(galleryProjectMatches(rows[1],'Борис'));
assert.ok(galleryProjectMatches(rows[0],'201'));assert.equal(galleryProjectTitle(rows[0]),'Алиса');
assert.ok(galleryProjectDetails(rows[0]).includes('Съёмка №1'));assert.ok(galleryProjectDetails(rows[0]).includes('комната 201'));
const authoritative=project(99,{folderPath:[hotel,october,day,{key:'photographer:4',label:'Фактический фотограф',sort:'Фактический фотограф'}]});
assert.equal(galleryFolderPath(authoritative)[3].label,'Фактический фотограф');
console.log('PASS: calendar month/day order, years, photographer groups, missing metadata, distinct same-guest shoots and global folder search');
