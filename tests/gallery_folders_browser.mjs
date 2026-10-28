/* Synthetic local UI fixture: no production requests, tokens or storage. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=path.resolve('app/webapp');
const shot=(id,change={})=>({id,title:'Семейная съёмка',hotelId:1,hotel:'Мира',photographerId:2,photographer:'Анна',client:'Алиса',date:'2026-10-02',time:'12:00',room:'201',guestCount:2,photoCount:0,bytes:0,views:0,downloads:0,createdAt:'2026-10-02T09:00:00',expiresAt:null,clientUrl:null,...change});
let items=[shot(1),shot(2,{photographerId:3,photographer:'Борис',client:'Боб'}),shot(3,{date:'2026-11-10'}),shot(4,{date:'2026-10-10'}),shot(5,{hotelId:2,hotel:'Альфа'}),shot(6,{date:'2025-10-02'}),shot(7),shot(8,{hotelId:null,hotel:'',photographerId:null,photographer:'',date:null,client:''}),shot(9,{trashed:true})];
const server=http.createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost');
  let body,type='text/javascript';
  if(url.pathname==='/app/'){
   type='text/html';body=`<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app/css/galleries.css"><div id="app"></div><script type="module">import {loadGalleries,galleriesView,mountGalleries} from '/app/js/galleries.js';await loadGalleries();document.querySelector('#app').innerHTML=galleriesView({user:{name:'Синтетический тест'},permissions:{}});mountGalleries();</script></html>`;
  }else if(url.pathname==='/app/js/api.js')body=`export async function api(path,options={}){const response=await fetch('/fixture'+path,{...options,body:options.body?JSON.stringify(options.body):undefined});return response.json();}`;
  else if(url.pathname==='/app/js/outbox.js')body='export async function enqueueFilesOneByOne(){throw Error("No uploads in navigation fixture");}';
  else if(url.pathname==='/fixture/galleries'){type='application/json';body=JSON.stringify({items,owner:false,canPurge:false,storage:{bytes:0},defaults:{days:180}});}
  else if(/^\/fixture\/delivery\/\d+$/.test(url.pathname)){type='application/json';const id=Number(url.pathname.split('/').pop()),p=items.find(x=>x.id===id);body=JSON.stringify({...p,bookingId:id,photos:[],title:p.client,canEdit:true});}
  else if(/^\/fixture\/galleries\/\d+\/preferences$/.test(url.pathname)){
   type='application/json';let raw='';for await(const chunk of req)raw+=chunk;
   const p=items.find(x=>x.id===Number(url.pathname.split('/')[3]));Object.assign(p,JSON.parse(raw));body='{"ok":true}';
  }else if(url.pathname.startsWith('/app/')){
   const file=path.resolve(base,url.pathname.slice(5));assert.ok(file.startsWith(base+path.sep));body=await fs.readFile(file);type=file.endsWith('.css')?'text/css':'text/javascript';
  }else{res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':type});res.end(body);
 }catch(e){res.writeHead(500);res.end(String(e));}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`,browser=await chromium.launch({headless:true});
const output=process.env.GALLERY_HIERARCHY_QA_DIR;
try{
 for(const width of [1440,390]){
  const page=await browser.newPage({viewport:{width,height:width===390?844:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
  await page.goto(origin+'/app/');
  const folder=label=>page.locator('.gallery-folder').filter({has:page.locator('strong',{hasText:new RegExp('^'+label+'$')})});
  const heading=()=>page.locator('.folder-heading h2');
  await heading().waitFor();assert.equal(await heading().textContent(),'Отели');
  assert.equal(await page.locator('.gallery-folder').count(),3);
  assert.equal(await page.locator('.gallery-folder strong').last().textContent(),'Отель не указан');
  if(output){await fs.mkdir(output,{recursive:true});await page.screenshot({path:path.join(output,`gallery-hotels-${width}.png`),fullPage:true});}
  await folder('Мира').click();assert.equal(await heading().textContent(),'Месяцы');
  assert.deepEqual(await page.locator('.gallery-folder strong').allTextContents(),['Ноябрь 2026','Октябрь 2026','Октябрь 2025']);
  await page.locator('[data-gp=sort]').click();
  assert.deepEqual(await page.locator('.gallery-folder strong').allTextContents(),['Октябрь 2025','Октябрь 2026','Ноябрь 2026']);
  await folder('Октябрь 2026').click();assert.deepEqual(await page.locator('.gallery-folder strong').allTextContents(),['2','10']);
  await folder('2').click();assert.deepEqual(await page.locator('.gallery-folder strong').allTextContents(),['Анна','Борис']);
  await folder('Анна').click();assert.equal(await heading().textContent(),'Съёмки гостей');
  const visibleProjects=page.locator(width===390?'[data-project-cards] [data-gp=open]':'[data-project-rows] [data-gp=open]');
  assert.equal(await visibleProjects.count(),2);assert.ok((await visibleProjects.allTextContents()).every(x=>x.includes('Алиса')));
  assert.ok((await visibleProjects.allTextContents()).some(x=>x.includes('Съёмка №1')));
  assert.ok((await visibleProjects.allTextContents()).some(x=>x.includes('Съёмка №7')));
  assert.ok((await visibleProjects.first().textContent()).includes('комната 201'));
  if(output){await fs.mkdir(output,{recursive:true});await page.screenshot({path:path.join(output,`gallery-guests-${width}.png`),fullPage:true});}
  await visibleProjects.first().click();await page.locator('[data-gv=back]').click();await heading().waitFor();
  assert.equal(await heading().textContent(),'Съёмки гостей');assert.equal(await visibleProjects.count(),2);
  items.push(shot(width,{client:'Новая съёмка'}));
  await page.locator('[data-gp=refresh]').click();await visibleProjects.filter({hasText:'Новая съёмка'}).first().waitFor();
  assert.equal(await page.locator('.folder-breadcrumbs [aria-current=location]').textContent(),'Анна');
  await page.locator('[data-gp=folder-back]').click();assert.equal(await heading().textContent(),'Фотографы');
  await folder('Борис').click();assert.ok((await visibleProjects.first().textContent()).includes('Боб'));
  await page.locator('[data-gp=folder-crumb][data-depth="1"]').click();assert.equal(await heading().textContent(),'Месяцы');
  await page.locator('[data-project-search]').fill('Боб');assert.equal(await heading().textContent(),'Результаты поиска');assert.equal(await visibleProjects.count(),1);
  assert.ok((await visibleProjects.textContent()).includes('Мира / Октябрь 2026 / 2 / Борис'));
  await page.locator('[data-project-search]').fill('');await page.locator('[data-gp=folder-root]').click();
  await page.locator('[data-gp-mode=trash]').click();assert.equal(await page.locator('.gallery-folder').count(),1);
  await folder('Мира').click();await folder('Октябрь 2026').click();await folder('2').click();await folder('Анна').click();assert.equal(await visibleProjects.count(),1);
  await page.locator('[data-gp-mode=all]').click();assert.ok(await visibleProjects.count()>=3);
  const heart=page.locator('[data-project-rows] [data-gp=favorite][data-id="1"]');
  if(width===1440){await heart.click();await page.locator('[data-project-rows] [data-gp=favorite][data-id="1"][aria-pressed=true]').waitFor();}
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.deepEqual(errors,[]);await page.close();
  items=items.filter(x=>x.id!==width);
 }
 console.log('PASS Chromium desktop/mobile: hotel → month/year → day → photographer → separate guest shoots; calendar sorting, breadcrumbs/back, refresh, global search, trash and favorites');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
