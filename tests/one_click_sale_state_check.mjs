import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync(new URL('../app/webapp/js/sale-files.js',import.meta.url),'utf8');
const {saleSaveState}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const rows=[
 {kind:'sale_start',status:'synced',result:{draftId:1}},
 {kind:'sale_receipt',status:'synced'},
 {kind:'sale_counts',status:'synced'},
 {kind:'sale_selected',status:'local',hasBlob:true},
 {kind:'sale_complete',status:'local'},
];
assert.equal(saleSaveState(rows).accepted,false,'locally selected or queued files are not server acceptance');
rows[3]={kind:'sale_selected',status:'synced',result:{photoId:8,saleId:9}};
assert.equal(saleSaveState(rows).accepted,false,'last-file automatic sale completion still requires final durable completion acknowledgement');
rows[4]={kind:'sale_complete',status:'synced',result:{saleId:9,gallery:{saleId:9,ready:false,state:'payment_pending',clientUrl:null}}};
assert.equal(saleSaveState(rows).accepted,true);
assert.equal(saleSaveState(rows).ready,false,'recorded sale must not expose a pending/unpaid gallery');
rows[4].result.gallery={saleId:9,ready:true,state:'ready',clientUrl:'https://photos.example/g/'+'a'.repeat(32),photoCount:1};
assert.equal(saleSaveState(rows).ready,true);
rows[1].status='local';
assert.equal(saleSaveState(rows).ready,false,'a future or stale completion cannot bypass a missing receipt acknowledgement');
rows[1].status='synced';rows.push(undefined);
assert.equal(saleSaveState(rows).accepted,false,'a missing durable dependency cannot be presented as success');
console.log('PASS: selected/queued/transferred/saved/paid-gallery are distinct and final ACK cannot bypass a missing predecessor');
