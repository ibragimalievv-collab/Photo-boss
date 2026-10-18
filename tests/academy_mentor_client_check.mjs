import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const listeners=new Map(),sent=[],sessions=[];
let current={configured:true,sources:[],history:[],skills:[]},failAsk=false,question='Как улучшить свет?',getFailure=false;
globalThis.document={addEventListener(name,fn){const handlers=listeners.get(name)||[];handlers.push(fn);listeners.set(name,handlers);},dispatchEvent(){return true;},querySelector(){return null;}};
globalThis.CustomEvent=class{constructor(type){this.type=type;}};
globalThis.FormData=class{get(){return question;}};
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{onLine:true}});
globalThis.mentorApi=async(path,options={})=>{
 if(path.startsWith('/academy/mentor?')){if(getFailure)throw Error('Сеть недоступна');return current;}
 if(path.startsWith('/academy/mentor/ask?')){sent.push(options.body);if(failAsk){failAsk=false;throw Error('Ответ не получен');}return {requestId:options.body.requestId,question:options.body.question,track:path.includes('booking')?'booking':'photographer',answer:{answer:'Прямой ответ',observations:[],steps:['Проверить свет'],exercise:'Снять пять кадров',limitations:[],sourceIds:[]},sources:[]};}
 if(path==='/academy/development')return {sessions:[...sessions]};
 if(path==='/academy/sales-training'){sessions.push({id:101,status:'ACTIVE',revision:1,clientType:options.body.clientType,transcript:[{role:'employee',text:'Мой сохранённый вопрос'}]});return {id:101};}
 throw Error('Unexpected '+path);
};
let source=await readFile(new URL('../app/webapp/js/academy-mentor.js',import.meta.url),'utf8');
source=source.replace("import {api} from './api.js';","const api=globalThis.mentorApi;")
 .replace("import {esc} from './domain.js';","const esc=value=>String(value??'').replaceAll('<','&lt;');")
 .replace("import {captureForms,clearForm} from './form-state.js';","const captureForms=()=>{},clearForm=()=>{};")
 .replace("import {realtimeControls} from './realtime-training.js';","const realtimeControls=(s,mode)=>`<section>LIVE ${s.id} ${mode} transcript</section>`;");
const mentor=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const submit=async()=>{for(const fn of listeners.get('submit')||[])await fn({target:{id:'academyMentorForm'},preventDefault(){}});};
const click=async attribute=>{const button={hasAttribute(name){return name===attribute;},dataset:{}};for(const fn of listeners.get('click')||[])await fn({target:{closest(){return button;}}});await new Promise(resolve=>setImmediate(resolve));};
mentor.resetAcademyMentor(1001);await mentor.loadAcademyMentor();
await submit();await submit();assert.equal(sent.length,2);assert.notEqual(sent[0].requestId,sent[1].requestId,'A successful question needs a fresh receipt so new photo evidence can invalidate the answer cache');
failAsk=true;question='Почему нерезко?';await submit();await submit();assert.equal(sent[2].requestId,sent[3].requestId,'A failed or lost response retries the same durable request');

await click('data-mentor-voice');assert.equal(mentor.academyMentorSessionId(),101);assert.match(mentor.academyMentorScreen(),/LIVE 101 coach transcript/);assert.match(mentor.academyMentorScreen(),/Мой сохранённый вопрос/);
await click('data-mentor-voice');assert.equal(sessions.length,1,'Opening the mentor reuses an active session');

getFailure=true;const changing=mentor.loadAcademyMentor('booking');assert.doesNotMatch(mentor.academyMentorScreen(),/Почему нерезко/,'Old track answers must disappear immediately');await changing;assert.doesNotMatch(mentor.academyMentorScreen(),/Почему нерезко/);
getFailure=false;current={};await mentor.loadAcademyMentor('booking');assert.doesNotThrow(()=>mentor.academyMentorScreen(),'Empty API stubs must normalize to arrays');
mentor.resetAcademyMentor(1002);assert.equal(mentor.academyMentorSessionId(),null,'Account switch must discard the previous account voice session');
console.log('PASS: fresh success receipts, lost-response retry, direct reusable voice, saved text, track isolation, empty snapshots and account reset');
