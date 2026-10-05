import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/webapp/js/install.js',import.meta.url),'utf8');
function scenario({ua='Chrome',platform='',touch=0,standalone=false,clipboard=true,telegram=false}={}){
 const elements={},events={},display={matches:standalone,addEventListener:(name,fn)=>events.display=fn};
 for(const id of ['installBar','installApp','installDialog','installStatus','installInstructions','installEmbedded','closeInstall','installLink','openInstallLink','copyInstallLink']){
  elements[id]={hidden:true,disabled:false,open:false,textContent:'',innerHTML:'',value:'',href:'',listeners:{},
   addEventListener(name,fn){this.listeners[name]=fn;},showModal(){this.open=true;},close(){this.open=false;},select(){this.selected=true;}};
 }
 const writes=[];
 const context=vm.createContext({URL,console,
  document:{querySelector:selector=>elements[selector.slice(1)]},
  navigator:{userAgent:ua,platform,maxTouchPoints:touch,standalone,clipboard:clipboard?{writeText:async text=>writes.push(text)}:undefined},
  window:{location:{origin:'https://photo-boss.onrender.com'},Telegram:telegram?{WebApp:{initData:'signed'}}:undefined,matchMedia:()=>display,addEventListener:(name,fn)=>events[name]=fn}});
 vm.runInContext(source,context);
 return {elements,events,display,writes,click:id=>elements[id].listeners.click()};
}
const desktop=scenario();
assert.equal(desktop.elements.installBar.hidden,false);
await desktop.click('installApp');assert.equal(desktop.elements.installDialog.open,true);
assert.match(desktop.elements.installInstructions.innerHTML,/Компьютер/);
await desktop.click('copyInstallLink');assert.deepEqual(desktop.writes,['https://photo-boss.onrender.com/app/']);
assert.equal(desktop.elements.openInstallLink.href,desktop.writes[0]);
await desktop.click('closeInstall');assert.equal(desktop.elements.installDialog.open,false);
const iphone=scenario({ua:'iPhone Safari'});await iphone.click('installApp');assert.match(iphone.elements.installInstructions.innerHTML,/На экран Домой/);
const ipad=scenario({platform:'MacIntel',touch:5});await ipad.click('installApp');assert.match(ipad.elements.installInstructions.innerHTML,/iPhone и iPad/);
const android=scenario({ua:'Android; wv)',telegram:true,clipboard:false});await android.click('installApp');assert.match(android.elements.installInstructions.innerHTML,/Android/);assert.equal(android.elements.installEmbedded.hidden,false);
await android.click('copyInstallLink');assert.equal(android.elements.installLink.selected,true);assert.match(android.elements.installStatus.textContent,/выделенную/);
assert.equal(scenario({standalone:true}).elements.installBar.hidden,true);
let prompts=0,prevented=0;
const native=scenario();
native.events.beforeinstallprompt({preventDefault:()=>prevented++,prompt:async()=>prompts++,userChoice:Promise.resolve({outcome:'accepted'})});
await native.click('installApp');assert.equal(prompts,1);assert.equal(prevented,1);
assert.equal(native.elements.installBar.hidden,false); // Accepted is not installed yet.
native.events.appinstalled();assert.equal(native.elements.installBar.hidden,true);
await native.click('installApp');assert.equal(prompts,1);
const cancelled=scenario();cancelled.events.beforeinstallprompt({preventDefault(){},prompt:async()=>{},userChoice:Promise.resolve({outcome:'dismissed'})});
await cancelled.click('installApp');assert.equal(cancelled.elements.installDialog.open,true);assert.equal(cancelled.elements.installApp.disabled,false);
const failed=scenario();failed.events.beforeinstallprompt({preventDefault(){},prompt:async()=>{throw Error('Unavailable');}});
await failed.click('installApp');assert.equal(failed.elements.installDialog.open,true);
desktop.display.matches=true;desktop.events.display();assert.equal(desktop.elements.installBar.hidden,true);
console.log('PASS: native prompt, dismissal, failed prompts, installed detection, iPhone/iPad/Android instructions, embedded browser and safe sharing');
