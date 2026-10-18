import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../app/webapp/js/outbox.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replaceAll('export ', '') + '\nasync function writeOperations(items){for(const row of items)await localAction(\"operations\",\"readwrite\",store=>store.put(row));return true;}';
const rows = new Map();
const add = (key, kind, blob = null) => rows.set(key, {
  key, userId: 42, kind, date: '2026-10-13', data: {shooting: {id: 7}},
  filename: blob ? 'photo.jpg' : '', blob, createdAt: 1, order: rows.size, status: 'local',
});
add('receipt', 'booking_receipt', new Blob(['fake receipt'], {type: 'image/jpeg'}));
add('photo', 'shoot_photo', new Blob(['original image'], {type: 'image/jpeg'}));
add('finish', 'shoot_complete');

let receiptFixed = false;
const calls = [];
const api = async (_path, {body}) => {
  const operation = body instanceof FormData ? JSON.parse(body.get('operation')) : body;
  calls.push(operation.kind);
  if (operation.kind === 'booking_receipt' && !receiptFixed) {
    const error = new Error('Telegram rejected this receipt');
    error.status = 503;
    throw error;
  }
  return {photoId: operation.kind === 'shoot_photo' ? 10 : null};
};
const localAction = async (_store, _mode, fn) => fn({
  get: key => rows.get(key),
  put: row => rows.set(row.key, row),
});
const context = vm.createContext({
  rows, api, localAction, Blob, FormData, Promise, Event, Date, Error,
  navigator: {onLine: true},
  window: {dispatchEvent() {}, addEventListener() {}},
  document: {hidden: false, addEventListener() {}},
  setTimeout() {return 1;}, clearTimeout() {},
});
vm.runInContext(code, context);
vm.runInContext(`userId = 42;
  outboxRows = async () => [...rows.values()].map(r => ({...r, hasBlob: Boolean(r.blob), blob: null}))
    .sort((a,b) => a.createdAt-b.createdAt || a.order-b.order);`, context);

const first = await vm.runInContext('flushOutbox()', context);
assert.equal(first.completed, 2);
assert.equal(first.temporary, 1);
assert.equal(rows.get('receipt').status, 'local');
assert.ok(rows.get('receipt').blob, 'failed receipt retains original image bytes');
assert.equal(rows.get('photo').status, 'synced');
assert.equal(rows.get('photo').blob, null);
assert.equal(rows.get('finish').status, 'synced');
assert.deepEqual(calls, ['booking_receipt','shoot_photo','shoot_complete']);

receiptFixed = true;
const second = await vm.runInContext('retryOperation("receipt")', context);
assert.equal(second.completed, 1);
assert.equal(rows.get('receipt').status, 'synced');
assert.equal(rows.get('receipt').blob, null);
console.log('PASS: bad receipt leaves its bytes intact and does not block full shoot photos');
