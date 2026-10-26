"""Actual IndexedDB recovery of a rejected Disk reference, preserving other writes."""
import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
API = """export async function api(path,{body}){
 window.sent.push(body);
 if(body.kind==='sale_disk_selected')return {ok:true,draftId:7,photoId:9};
 if(body.kind==='sale_complete')return {ok:true,saleId:22};
 throw new Error('Unexpected operation');
}"""


async def main():
    async def static(request):
        name = request.match_info['name']
        if name == 'api.js':
            return web.Response(text=API, content_type='text/javascript')
        if name not in {'outbox.js', 'localstore.js'}:
            raise web.HTTPNotFound()
        return web.FileResponse(ROOT / 'app' / 'webapp' / 'js' / name)

    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text='<title>Isolated Disk recovery</title>', content_type='text/html'))
    app.router.add_get('/js/{name}', static)
    server = TestServer(app)
    await server.start_server()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(str(server.make_url('/')))
                result = await page.evaluate("""async()=>{
                 const box=await import('/js/outbox.js'),local=await import('/js/localstore.js');
                 const check=(condition,message)=>{if(!condition)throw Error(message);};
                 const rejection='Папка съёмки изменилась. Откройте её и выберите фотографии заново.';
                 const base={userId:42,date:'2026-10-26',generation:'',salesGeneration:'',createdAt:1,blob:null};
                 const start={...base,key:'start',kind:'sale_start',data:{booking:{id:5}},status:'synced',result:{draftId:7}};
                 const draft={key:'start'};
                 const stale={...base,key:'stale-photo',kind:'sale_disk_selected',data:{draft,revision:'old',path:'disk:/old/a.jpg'},
                  status:'error',errorStatus:409,error:rejection,order:4};
                 const rows=[start,
                  {...base,key:'receipt',kind:'sale_receipt',data:{draft},status:'synced',result:{receiptId:3},order:1},
                  {...base,key:'counts',kind:'sale_counts',data:{draft,total:150,sold:2,price:'400'},status:'synced',order:2},
                  {...base,key:'accepted-photo',kind:'sale_selected',data:{draft},status:'synced',result:{photoId:8},order:3},stale,
                  {...base,key:'complete',kind:'sale_complete',data:{draft,price:'400'},status:'local',order:5}];
                 window.sent=[];await box.setOutboxUser(42);
                 await local.writeOperations(rows,{add:true});
                 await box.flushOutbox();check(window.sent.length===0,'Rejected frame must block completion');
                 const before=JSON.stringify(rows.filter(row=>!['stale-photo','complete'].includes(row.key)));
                 await box.replaceDiskSelection('stale-photo',{revision:'new',path:'disk:/new/b.jpg'},7);
                 for(let i=0;i<100;i++){
                  if((await box.outboxRows()).find(row=>row.key==='complete').status==='synced')break;
                  await new Promise(done=>setTimeout(done,10));
                 }
                 const saved=await box.outboxRows();
                 check(saved.find(row=>row.key==='complete').status==='synced','Same completion must recover');
                 check(window.sent.length===2&&window.sent[0].key==='stale-photo'&&window.sent[1].key==='complete','Only original photo and completion keys may send');
                 check(window.sent[0].data.draft.key==='start'&&window.sent[0].data.revision==='new','Draft identity must survive replacement');
                 const retained=[];for(const row of rows.filter(row=>!['stale-photo','complete'].includes(row.key)))retained.push(await local.localAction('operations','readonly',s=>s.get(row.key)));
                 check(JSON.stringify(retained)===before,'Accepted receipt/counts/photos must remain byte-for-byte unchanged');
                 const protectedCases=[
                  {status:'synced',result:{photoId:9}}, {status:'syncing'},
                  {status:'local',errorStatus:503}, {errorStatus:403}, {errorStatus:409,error:'Ключ уже использован для других данных.'},
                  {result:{photoId:9}}, {userId:99}, {generation:'before-reset'}, {salesGeneration:'before-sales-reset'}];
                 let refused=0;
                 for(const extra of protectedCases){
                  await local.localAction('operations','readwrite',s=>s.clear());
                  const protectedRow={...stale,...extra};await local.localAction('operations','readwrite',s=>s.put(protectedRow));
                  await local.localAction('operations','readwrite',s=>s.put(start));
                  const original=JSON.stringify(await local.localAction('operations','readonly',s=>s.get(stale.key)));
                  try{await box.replaceDiskSelection(stale.key,{revision:'new',path:'disk:/new/b.jpg'},7);}catch{refused++;}
                  check(JSON.stringify(await local.localAction('operations','readonly',s=>s.get(stale.key)))===original,'Protected operation mutated');
                 }
                 check(refused===protectedCases.length,'Every accepted, uncertain or foreign operation must be refused');
                 await local.localAction('operations','readwrite',s=>s.clear());
                 await local.writeOperations([start,stale],{add:true});
                 for(const [photo,id] of [[{revision:'new',path:'disk:/new/b.jpg'},8],[{revision:'old',path:'disk:/old/a.jpg'},7]]){
                  let denied=false;try{await box.replaceDiskSelection(stale.key,photo,id);}catch{denied=true;}
                  check(denied,'Wrong draft and stale source must be refused');
                 }
                 check(window.sent.length===2,'Rejected corrections must never send requests');
                 return {recovered:true,sameKeys:true,protectedCases:refused};
                }""")
                assert result == {'recovered': True, 'sameKeys': True, 'protectedCases': 9}
                assert not errors, errors
                print('PASS: real IndexedDB stale Disk reference reselects under its same draft/key; receipt/counts/accepted photos remain unchanged; accepted, uncertain, foreign and stale corrections refused')
            finally:
                await browser.close()
    finally:
        await server.close()


if __name__ == '__main__':
    asyncio.run(main())
