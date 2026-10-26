import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=await readFile(new URL('../app/webapp/js/practice.js',import.meta.url),'utf8');
const start=source.indexOf('async function preparePhoto(file,');
const end=source.indexOf("\npanel.addEventListener('change'",start);
assert.ok(start>=0&&end>start);
const prepare=Function(source.slice(start,end)+';return preparePhoto;')();
const original=await readFile(new URL('../app/assets/training/woman/01.jpg',import.meta.url));
const file=new File([original],'camera.jpg',{type:'image/jpeg'});
const payload=await prepare(file);
assert.equal(payload.type,'image/jpeg');
assert.deepEqual(Buffer.from(await payload.arrayBuffer()),original,'Original bytes and EXIF must remain identical');
assert.ok(!(payload instanceof File),'Use a Blob for consistent multipart transports');
for(const type of ['', 'application/octet-stream']){
 const picked=await prepare(new File([original],'Camera.JPG',{type}));
 assert.deepEqual(Buffer.from(await picked.arrayBuffer()),original,'Cloud picker JPEG without MIME must preserve bytes');
}
await assert.rejects(()=>prepare(new File([original],'camera.HEIC',{type:'image/heic'})),/HEIC\/HEIF.*JPEG/);
await assert.rejects(()=>prepare(new File([original],'spoof.jpg',{type:'image/svg+xml'})),/JPEG, PNG или WebP/);
for(const size of [8*1024*1024+1,32*1024*1024]){
 const large=new File([new Uint8Array(size)],'large.jpg',{type:'image/jpeg'});
 large.arrayBuffer=()=>{throw new Error('Large originals must not need a second full memory buffer');};
 const uploaded=await prepare(large);
 assert.equal(uploaded.size,size,'Camera originals larger than 8 and 25 MiB must be accepted');
}
await assert.rejects(()=>prepare(new File([original],'camera.jpg',{type:'image/jpeg'}),1),/предел сервера/);
console.log('PASS: original JPEG byte preservation, streaming large originals and honest server bound');
