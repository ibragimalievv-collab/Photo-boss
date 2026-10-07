import asyncio,json,mimetypes,re,io
from pathlib import Path
from datetime import datetime,timezone
from playwright.async_api import async_playwright
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[2]; OUT=ROOT/'guide-screens';OUT.mkdir(parents=True,exist_ok=True)
TODAY=datetime.now(timezone.utc).date().isoformat()
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(args=['--no-sandbox'])
  context=await browser.new_context(viewport={'width':1280,'height':900},device_scale_factor=1,service_workers='block')
  await context.add_init_script(Path(__file__).with_name('demo-bootstrap.js').read_text())
  page=await context.new_page(); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  async def intercept(route):
   path=route.request.url.split('photos.example')[-1];plain=path.split('?')[0]
   if not route.request.url.startswith('https://photos.example'):
    return await route.fulfill(body='',content_type='application/javascript')
   if path.startswith('/api/miniapp'):
    endpoint=path[len('/api/miniapp'):];e=endpoint.split('?')[0];data=None
    if e=='/shoot-control':
     data={'from':TODAY,'to':TODAY,'counts':{'shoot':2,'sale':2,'payment':1,'delivery':3},'items':[{'id':501+i,'client':f'Учебный гость {i+1}','date':TODAY,'time':'14:30','hotel':'Учебный отель','room':str(200+i),'photographer':'Демо Фотограф','tasks':(['shoot'] if i==0 else ['sale','payment','delivery'] if i==1 else ['delivery']),'shot':i>0,'sold':i>1,'paymentStatus':'PAID' if i>1 else 'PENDING','delivered':False,'openedAt':None,'canGallery':True,'guestConfirmed':i>0,'rescheduleRequested':False,'cancelled':False} for i in range(3)],'hotels':[{'name':'Учебный отель','booked':6,'shot':4,'sold':2}],'photographers':[{'name':'Демо Фотограф','booked':3,'shot':2,'sold':1}]}
    elif e=='/delivery/501':data={'title':'Учебный альбом','photographer':'Демо Фотограф','canEdit':True,'published':True,'passwordEnabled':False,'expiresAt':None,'photos':[{'id':1,'name':'Учебный снимок.png'}],'botUrl':'https://t.me/PhotoBossTest?start=gallery_'+'a'*32,'clientUrl':'https://photos.example/g/'+'a'*32,'botStartedAt':None,'botLinkSentAt':None,'openedAt':None,'downloadedAt':None,'handedAt':None}
    elif e.endswith('/qr') or '/photos/' in e:
     im=Image.new('RGB',(500,350),'#c7d9d6');ImageDraw.Draw(im).text((40,150),'PHOTO BOSS / TRAINING',fill='#203843');b=io.BytesIO();im.save(b,format='PNG');return await route.fulfill(body=b.getvalue(),content_type='image/png')
    elif e=='/booking-photographers':data={'items':[{'id':3,'name':'Демо Фотограф'},{'id':4,'name':'Тестовый Фотограф'}]}
    elif e=='/workflow':data={'date':TODAY,'actorId':1,'canBook':True,'hotels':[{'id':1,'name':'Учебный отель'}],'packages':[{'id':1,'name':'Семейная съёмка'}],'bookings':[{'id':501,'date':TODAY,'time':'14:30','room':'201','status':'READY_FOR_SALE','canUpload':True,'fullUploaded':False,'shootingId':1,'price':'400','files':[],'edits':[]}]}
    elif e=='/workday':data={'date':TODAY,'timezone':'Europe/Moscow','closed':False,'canReport':True,'canConfigure':True,'note':'','sales':[],'configItems':[],'summary':{'bookings':6,'cancelled':0,'sales':2,'revenue':1200000,'unpaid':1},'items':[{'key':'photos','title':'Проверить загрузку готовых фотографий','done':False}]}
    elif e=='/delivery-contacts':data={'total':2,'subscribers':1,'items':[{'name':'Учебный гость','username':'training_guest','phone':None,'subscribed':True,'blocked':False}],'campaigns':[]}
    elif e=='/hr':data={'employees':[{'id':3,'name':'Демо Фотограф','active':True}],'items':[],'stages':{}}
    elif e=='/attendance/pending':data={'items':[]}
    elif e=='/onboarding':data={'steps':[{'role':'OWNER','slug':'start','title':'Рабочий день','body':'Проверьте съёмки и ответственных сотрудников.','route':'home','completed':False}],'quiz':[],'attempts':[],'progress':{'completed':0,'total':1,'percent':0},'taskProgress':[{'title':'Разобраться с рабочими операциями','route':'workflow','completed':False,'required':True}]}
    elif e=='/academy/development':data={'configured':False,'rules':'Предлагайте фотографии спокойно, объясняйте ценность и стоимость.','scenarios':[{'title':'Цена','opening':'Почему так дорого?','advice':'Покажите результат и объясните пакет.','type':'price'}],'sessions':[],'reviews':[]}
    elif e=='/events':data={'items':[],'preferences':{'daily':False,'critical':False}}
    elif e=='/insights':data={'from':TODAY,'to':TODAY,'previous':{'from':TODAY,'to':TODAY},'metrics':{'bookings':6,'sales':2,'revenue':1200000},'changes':{k:{'absolute':0,'percent':None} for k in ['bookings','sales','revenue']},'note':'Учебные данные для инструкции','basis':'Показатели выбранного периода','flags':[],'employees':[],'hotels':[],'expenseOptions':{'categories':{'OTHER':'Прочее'},'today':TODAY,'hotels':[],'employees':[]},'paidMovements':[],'paidMovementCount':0}
    if data is None:
     try:data=await page.evaluate('async x=>await window.pbDemoApi(x.path,{method:x.method,body:x.body})',{'path':endpoint,'method':route.request.method,'body':json.loads(route.request.post_data or '{}')})
     except Exception:data={'items':[]}
    return await route.fulfill(body=json.dumps(data),content_type='application/json')
   if plain.startswith('/app/'):
    rel=plain[5:] or 'index.html';file=ROOT/'app/webapp'/rel
    if file.is_file():return await route.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
   return await route.fulfill(body='',content_type='application/javascript' if plain.endswith('.js') else 'text/plain')
  await context.route('**/*',intercept)
  await page.goto('https://photos.example/app/');await page.wait_for_timeout(1500)
  for name in ['home','shootings','schedule','workflow','control','contacts','finance','insights','team','academy','development','onboarding','workday','audit','profile','more']:
   await page.evaluate('(name)=>document.querySelector(`[data-go="${name}"]`)?.click()',name)
   # Use the module's own navigation button, including destinations in More.
   if await page.locator('body').evaluate('(el)=>location.hash') != '#'+name:
    await page.evaluate('(name)=>{let b=document.createElement("button");b.dataset.go=name;document.body.append(b);b.click();b.remove();}',name)
   await page.wait_for_timeout(700)
   await page.screenshot(path=str(OUT/f'{name}.png'))
   print(name,await page.locator('#app').inner_text() if False else (await page.locator('#app').inner_text())[:60])
  await page.evaluate('()=>{let b=document.createElement("button");b.dataset.galleryBooking="501";document.body.append(b);b.click();b.remove();}')
  await page.wait_for_timeout(1000);await page.screenshot(path=str(OUT/'gallery.png'))
  assert await page.locator('.delivery-sheet h2').inner_text()=='Галерея съёмки №501'
  print('BROWSER_ERRORS',errors)
  (OUT/'browser-report.json').write_text(json.dumps({'errors':errors},ensure_ascii=False))
  assert not errors, errors
  await browser.close()
asyncio.run(main())
