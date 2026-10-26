"""Slow photo requests cannot freeze bookings or attendance in real IndexedDB."""
import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
API = """export async function api(path,{body,timeoutMs}){
 const operation=body instanceof FormData?JSON.parse(body.get('operation')):body;
 const bytes=body instanceof FormData?await body.get('file').text():null;
 window.sent.push({operation,path,timeoutMs,bytes});
 window.activeRequests++;window.maximumRequests=Math.max(window.maximumRequests,window.activeRequests);
 try{
  if(window.slowKeys.has(operation.key))await new Promise(resolve=>window.finishUploads.set(operation.key,resolve));
  if(operation.kind==='booking')return {bookingId:81};
  return {ok:true,photoId:9,receiptId:5};
 }finally{window.activeRequests--;}
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
    app.router.add_get('/', lambda request: web.Response(text='<title>Isolated outbox concurrency</title>', content_type='text/html'))
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
                 const until=async(test,message)=>{
                  for(let i=0;i<200;i++){if(await test())return;await new Promise(done=>setTimeout(done,10));}
                  throw Error(message);
                 };
                 const row=key=>local.localAction('operations','readonly',store=>store.get(key));
                 const date='2026-10-26',photo=()=>new Blob(['untouched camera or sale original'],{type:'image/jpeg'});
                 window.sent=[];window.activeRequests=0;window.maximumRequests=0;
                 window.slowKeys=new Set(['slow-sale','slow-arrival']);window.finishUploads=new Map();
                 await box.setOutboxUser(42);
                 await box.enqueueBatch([{key:'slow-sale',kind:'sale_selected',date,data:{draft:{id:7}},blob:photo()}]);
                 await until(()=>window.finishUploads.has('slow-sale'),'Sale upload did not enter network');
                 await box.enqueueBatch([{key:'slow-arrival',kind:'attendance',date,data:{purpose:'start'},blob:photo()}]);
                 await until(()=>window.finishUploads.has('slow-arrival'),'Attendance must start during a slow sale upload');
                 await box.enqueueBatch([
                  {key:'new-booking',kind:'booking',date,data:{client_name:'Гость',hotel_id:1}},
                  {key:'new-receipt',kind:'booking_receipt',date,data:{booking:{key:'new-booking'}},blob:photo()},
                  {key:'departure',kind:'attendance',date,data:{purpose:'end'},blob:photo()}]);
                 await until(async()=> (await row('new-booking'))?.status==='synced','Booking froze behind media uploads');
                 check((await row('slow-sale')).status==='syncing','Sale must remain unresolved during booking acknowledgement');
                 check((await row('slow-arrival')).status==='syncing','Arrival must remain unresolved during booking acknowledgement');
                 check((await row('departure')).status==='local','Departure must wait for the original arrival');
                 check((await row('new-receipt')).status==='local','Receipt remains queued until the independent media worker is free');
                 check(await (await row('slow-sale')).blob.text()==='untouched camera or sale original','In-flight original bytes must remain durable');
                 const booking=window.sent.find(item=>item.operation.key==='new-booking');
                 check(booking.path==='/operations/sync'&&booking.timeoutMs===20000,'Booking uses a short JSON request');
                 check(window.maximumRequests===3,'At most one booking, one attendance and one media request may overlap');
                 for(let i=0;i<4;i++)await box.flushOutbox();
                 check(window.sent.filter(item=>item.operation.key==='new-booking').length===1,'Foreground retries must not duplicate the booking');
                 check(window.sent.filter(item=>item.operation.key==='slow-sale').length===1,'Do not duplicate in-flight media');
                 check(window.sent.filter(item=>item.operation.key==='slow-arrival').length===1,'Do not duplicate in-flight attendance');
                 window.finishUploads.get('slow-arrival')();
                 await until(async()=> (await row('departure')).status==='synced','Departure must follow arrival acknowledgement');
                 check((await row('slow-sale')).status==='syncing','Attendance must finish while sale remains in flight');
                 window.finishUploads.get('slow-sale')();
                 await until(async()=> (await box.outboxRows()).every(item=>item.status==='synced'),'All original keys must finish');
                 check(window.sent.map(item=>item.operation.key).join(',')==='slow-sale,slow-arrival,new-booking,departure,new-receipt','Dependencies and worker ordering changed');
                 check(window.sent.find(item=>item.operation.key==='new-receipt').operation.data.booking.key==='new-booking','Receipt keeps its original booking dependency');
                 const accepted=await box.outboxRows();
                 check(accepted.length===5&&accepted.every(item=>!item.hasBlob),'Acknowledged originals are released without creating extra operations');
                 await box.flushOutbox();check(window.sent.length===5,'Reload/foreground pass must not resubmit accepted operations');
                 for(const [kind,key] of [['checklist','rejected-checklist'],['attendance','rejected-camera']]){
                  await local.localAction('operations','readwrite',store=>store.put({
                   key,userId:42,generation:'',salesGeneration:'',kind,date,
                   data:kind==='attendance'?{purpose:'start'}:{itemKey:'removed',done:true},
                   status:'error',errorStatus:400,error:'Definitive rejected old evidence',
                   blob:photo(),filename:'retained-original.jpg',createdAt:Date.now()-100,order:0}));
                  const fresh=key+'-fresh-camera';
                  await box.enqueueBatch([{key:fresh,kind:'attendance',date,data:{purpose:'start'},blob:photo()}]);
                  await until(async()=> (await row(fresh)).status==='synced','A definitive old rejection froze a valid new attendance photo');
                  check((await row(key)).status==='error','Do not overwrite the rejected operation');
                  check(await (await row(key)).blob.text()==='untouched camera or sale original','Retain rejected original bytes');
                  check(!window.sent.some(item=>item.operation.key===key),'Do not automatically resubmit definitively rejected old evidence');
                 }
                 return {operations:accepted.length,maximumConcurrentRequests:window.maximumRequests,recoveryChecks:2};
                }""")
                assert result == {'operations': 5, 'maximumConcurrentRequests': 3, 'recoveryChecks': 2}, result
                assert not errors, errors
                print('PASS: real IndexedDB booking/arrival/departure stay usable during slow media and old definitive rejections; bounded concurrency, original keys/bytes and receipt order survive')
            finally:
                await browser.close()
    finally:
        await server.close()


if __name__ == '__main__':
    asyncio.run(main())
