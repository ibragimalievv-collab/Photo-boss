// Minimal IndexedDB stand-in for vm harnesses of outbox.js.
// outbox.update() opens one transaction over 'operations' and 'snapshots',
// reads both, maybe puts, then waits for oncomplete. Requests resolve in
// microtasks and the transaction completes right after its last request,
// like a real transaction with no further work queued.
export function fakeOpenLocal(stores, {clone = (value) => value} = {}) {
  return async () => ({
    close() {},
    transaction(_names, _mode) {
      let aborted = false, pending = 0, done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (aborted) tx.onabort?.(); else tx.oncomplete?.();
      };
      const settleLater = () => queueMicrotask(() => { if (!pending) queueMicrotask(finish); });
      const tx = {
        oncomplete: null, onerror: null, onabort: null, error: null,
        abort() { aborted = true; },
        objectStore(name) {
          const map = stores[name] ?? (stores[name] = new Map());
          return {
            get(key) {
              const request = {result: undefined, onsuccess: null, onerror: null};
              pending++;
              queueMicrotask(() => {
                request.result = clone(map.get(key));
                try { request.onsuccess?.(); } finally { pending--; settleLater(); }
              });
              return request;
            },
            put(value, key) { if (!aborted) map.set(key ?? value.key, clone(value)); return {}; },
            delete(key) { if (!aborted) map.delete(key); return {}; },
          };
        },
      };
      // A transaction with no request at all still completes.
      setImmediate(() => { if (!pending) finish(); });
      return tx;
    },
  });
}
