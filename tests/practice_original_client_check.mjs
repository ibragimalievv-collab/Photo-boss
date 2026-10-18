import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=await readFile(new URL('../app/webapp/js/practice.js',import.meta.url),'utf8');
const start=source.indexOf('async function preparePhoto(file)');
const end=source.indexOf("\npanel.addEventListener('change'",start);
assert.ok(start>=0&&end>start);
const prepare=Function(source.slice(start,end)+';return preparePhoto;')();
const original=await readFile(new URL('../app/assets/training/woman/01.jpg',import.meta.url));
const file=new File([original],'camera.jpg',{type:'image/jpeg'});
const payload=await prepare(file);
assert.equal(payload.type,'image/jpeg');
assert.deepEqual(Buffer.from(await payload.arrayBuffer()),original,'Original bytes and EXIF must remain identical');
assert.ok(!(payload instanceof File),'Use a memory-backed byte copy for consistent multipart transports');
for(const type of ['', 'application/octet-stream']){
 const picked=await prepare(new File([original],'Camera.JPG',{type}));
 assert.deepEqual(Buffer.from(await picked.arrayBuffer()),original,'Cloud picker JPEG without MIME must preserve bytes');
}
await assert.rejects(()=>prepare(new File([original],'camera.HEIC',{type:'image/heic'})),/HEIC\/HEIF.*JPEG/);
await assert.rejects(()=>prepare(new File([original],'spoof.jpg',{type:'image/svg+xml'})),/JPEG, PNG или WebP/);
await assert.rejects(()=>prepare(new File([new Uint8Array(8*1024*1024+1)],'large.jpg',{type:'image/jpeg'})),/8 МБ/);
console.log('PASS: original JPEG byte preservation, bounded multipart copy and explicit oversize rejection');
