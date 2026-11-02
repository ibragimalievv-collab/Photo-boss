// UI regression with a local stub; no Avito or production authentication calls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)('playwright');
const fields=['title','duties','pay','payout','schedule','location','housing','travel','training','equipment','contract','bonuses'];
const state={configured:true,workerReady:true,lastCheck:1,fields,
  vacancies:[{code:'test',payload:{title:'Тестовая вакансия',pay:'15%',schedule:'3/1',location:'Анапа'},revision:0,confirmed:false}],
  candidates:[{chat_id:'test-chat',vacancy_code:'test',status:'ACTIVE',profile:{name:'<img src=x onerror=alert(1)>'},transcript:[{direction:'in',text:'<script>alert(1)</script>'}]}],
  profileLabels:{name:'Имя'},note:'Тестовый диалог'};
const calls=[];
const server=createServer(async(req,res)=>{
  try{
    const path=new URL(req.url,'http://localhost').pathname;
    if(path==='/app/js/api.js'){res.setHeader('Content-Type','text/javascript');res.end("export const api=async(path,options={})=>{const r=await fetch('/api/miniapp'+path,{method:options.method||'GET',headers:{'Content-Type':'application/json'},body:options.body?JSON.stringify(options.body):undefined});const value=await r.json();if(!r.ok)throw new Error(value.error||'Ошибка');return value;};");return;}
    if(path.startsWith('/api/miniapp/')){
      let body='';for await(const chunk of req)body+=chunk;const payload=body?JSON.parse(body):null;calls.push({path,method:req.method,payload});
      let result=state;
      if(path.endsWith('/terms')){Object.assign(state.vacancies[0],payload,{revision:1});result={ok:true,revision:1};}
      else if(path.endsWith('/preview'))result={answer:'Тестовый ответ по условиям.',needsManager:false,sent:false};
      else if(path.endsWith('/control')){state.candidates[0].status=payload.action==='pause'?'HUMAN':'ACTIVE';result={ok:true};}
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));return;
    }
    if(path==='/app/css/styles.css'){res.setHeader('Content-Type','text/css');res.end('');return;}
    const file=path==='/app/hr-avito.html'?'app/webapp/hr-avito.html':path==='/app/js/hr-avito.js'?'app/webapp/js/hr-avito.js':null;
    if(!file){res.statusCode=404;res.end();return;}
    res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':'text/html');res.end(await readFile(file));
  }catch(error){res.statusCode=500;res.end(String(error));}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
try{
  const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.goto('http://127.0.0.1:'+server.address().port+'/app/hr-avito.html');
  await page.getByRole('heading',{name:'Кандидаты',exact:true}).waitFor();
  assert.equal(await page.locator('img,script:not([src])').count(),0,'Candidate text must not become markup');
  const details=page.locator('details.panel').first();await details.locator('summary').click();
  await details.getByRole('checkbox').check();await details.getByRole('button',{name:'Сохранить условия'}).click();
  await page.waitForFunction(()=>document.querySelector('#hr-status').textContent==='Условия сохранены.');
  assert.equal(state.vacancies[0].confirmed,true);
  await details.locator('summary').click();
  await details.getByPlaceholder('Тестовый вопрос кандидата').fill('Какая зарплата?');
  await details.getByRole('button',{name:'Проверить',exact:true}).click();
  await page.getByText('Сообщение не отправлялось.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Взять переписку на себя'}).click();
  await page.getByRole('button',{name:'Вернуть ассистенту'}).waitFor();
  assert.equal(state.candidates[0].status,'HUMAN');
  await page.getByRole('button',{name:'Вернуть ассистенту'}).click();
  await page.getByRole('button',{name:'Взять переписку на себя'}).waitFor();
  assert.equal(state.candidates[0].status,'ACTIVE');assert.deepEqual(errors,[]);
  assert.equal(calls.filter(c=>c.path.endsWith('/preview')).length,1);
  console.log('HR console UI passed: save conditions, preview, pause/resume, safe candidate text, mobile viewport.');
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
