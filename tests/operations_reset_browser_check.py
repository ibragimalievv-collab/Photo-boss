"""A server reset removes owned operations atomically in real IndexedDB."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            page_errors = []
            page.on('pageerror', lambda error: page_errors.append(str(error)))

            async def route(request):
                if request.request.url.endswith('/localstore.js'):
                    await request.fulfill(path=str(ROOT / 'app/webapp/js/localstore.js'), content_type='text/javascript')
                else:
                    await request.fulfill(body='<main>Reset isolation fixture</main>', content_type='text/html')

            await page.route('https://reset-fixture.invalid/**', route)
            await page.goto('https://reset-fixture.invalid/')
            result = await page.evaluate('''async()=>{
              const m=await import('/localstore.js');
              const put=(store,row)=>m.localAction(store,'readwrite',s=>s.put(row));
              const get=(store,key)=>m.localAction(store,'readonly',s=>s.get(key));
              await put('operations',{key:'old-sale',userId:42,kind:'sale_start',blob:new Blob(['private original'])});
              await put('operations',{key:'other-sale',userId:43,kind:'sale_start',blob:new Blob(['other original'])});
              await put('operations',{key:'old-expense',userId:42,kind:'cash_expense',data:{note:'retained'}});
              for(const path of ['/me','/workflow','/attendance','/dashboard','/onboarding','/academy','/academy/coach?track=booking']){
                await put('snapshots',{key:'1042:'+path,data:{marker:path}});
                await put('snapshots',{key:'1043:'+path,data:{marker:'other'}});
              }
              const first=await m.reconcileOperationsGeneration(42,1042,'reset-1');
              const isolated=!(await get('operations','old-sale'))&&(await get('operations','other-sale')).blob.size===14;
              const expenseKept=(await get('operations','old-expense')).data.note==='retained';
              const staleWrite=await m.writeOperations([{key:'old-sale',userId:42,kind:'sale_start'}]);
              const staleAbsent=!(await get('operations','old-sale'));
              const cleared=(await Promise.all(['/workflow','/attendance','/dashboard','/onboarding'].map(p=>get('snapshots','1042:'+p)))).every(r=>!r);
              const kept=(await Promise.all(['/me','/academy','/academy/coach?track=booking'].map(p=>get('snapshots','1042:'+p)))).every(Boolean);
              const foreign=(await get('snapshots','1043:/workflow')).data.marker==='other';
              await put('operations',{key:'fresh-sale',userId:42,generation:'reset-1',kind:'booking'});
              const repeated=await Promise.all([m.reconcileOperationsGeneration(42,1042,'reset-1'),m.reconcileOperationsGeneration(42,1042,'reset-1')]);
              const preserved=Boolean(await get('operations','fresh-sale'));
              const accepted=await m.writeOperations([{key:'fresh-safe',userId:42,generation:'reset-1',kind:'booking'}],{add:true});
              const lateWrite=m.writeOperations([{key:'late-old-status',userId:42,generation:'reset-1',kind:'sale_start'}]);
              const second=await m.reconcileOperationsGeneration(42,1042,'reset-2');
              await lateWrite;
              const recleared=!(await get('operations','fresh-sale'));
              const noLateWrite=!(await get('operations','late-old-status'));
              const laterWrite=await m.writeOperations([{key:'after-reset-old-status',userId:42,generation:'reset-1',kind:'sale_start'}]);
              const retainedExpenseWrite=await m.writeOperations([{key:'old-expense',userId:42,kind:'cash_expense',data:{note:'still retained'}}]);
              await put('operations',{key:'legacy-not-reset',userId:44,kind:'booking'});
              const empty=await m.reconcileOperationsGeneration(44,1044,'');
              const legacyKept=Boolean(await get('operations','legacy-not-reset'));
              return {first,isolated,expenseKept,staleWrite,staleAbsent,cleared,kept,foreign,repeated,preserved,accepted,second,recleared,noLateWrite,laterWrite,retainedExpenseWrite,empty,legacyKept};
            }''')
            assert result == {
                'first': True, 'isolated': True, 'expenseKept': True,
                'staleWrite': False, 'staleAbsent': True, 'cleared': True, 'kept': True,
                'foreign': True, 'repeated': [False, False], 'preserved': True,
                'accepted': True, 'second': True, 'recleared': True, 'noLateWrite': True,
                'laterWrite': False, 'retainedExpenseWrite': True, 'empty': False, 'legacyKept': True,
            }, result
            clone_result = await page.evaluate('''async()=>{
              const m=await import('/localstore.js');
              const get=key=>m.localAction('operations','readonly',store=>store.get(key));
              const row=(key,data)=>({key,userId:42,generation:'reset-2',kind:'sale_counts',data});
              await m.writeOperations([row('existing-safe',{total:1})]);
              const results=[];
              for(const add of [false,true]){
                const first=add?row('new-safe',{total:2}):row('existing-safe',{total:999});
                const invalid=row('uncloneable',{invalid(){}});
                try{await m.writeOperations([first,invalid],{add});results.push('unexpected success');}
                catch(error){results.push(error.name);}
              }
              const original=await get('existing-safe'),fresh=await get('new-safe'),invalid=await get('uncloneable');
              const recovered=await m.writeOperations([row('after-clone-error',{total:3})],{add:true});
              return {results,original:original.data.total,fresh:!!fresh,invalid:!!invalid,recovered};
            }''')
            assert clone_result == {'results': ['DataCloneError', 'DataCloneError'],
                                    'original': 1, 'fresh': False, 'invalid': False,
                                    'recovered': True}, clone_result
            assert not page_errors, page_errors
            print('PASS: real IndexedDB first adoption, atomic reset, account isolation, Academy preservation, repeated login, second reset and legacy compatibility')
            print('PASS: uncloneable add/put batches reject and roll back atomically without uncaught browser errors; later writes succeed')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
