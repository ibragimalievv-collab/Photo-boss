// Generated locally: no remote audio requests, recordings or device permissions.
let context=null,output=null,timer=null,deadline=null,ringing=null;
const notes=new Set();
const silenced=new Set();

function phrase(){
 if(!ringing||context?.state!=='running')return;
 const now=context.currentTime;
 for(const [index,frequency] of [523.25,659.25,783.99,659.25].entries()){
  const oscillator=context.createOscillator(),gain=context.createGain();
  const start=now+index*0.22;
  oscillator.type='sine';oscillator.frequency.value=frequency;
  gain.gain.setValueAtTime(0,start);
  gain.gain.linearRampToValueAtTime(0.18,start+0.015);
  gain.gain.exponentialRampToValueAtTime(0.001,start+0.19);
  oscillator.connect(gain);gain.connect(output);notes.add(oscillator);
  oscillator.onended=()=>{notes.delete(oscillator);oscillator.disconnect();gain.disconnect();};
  oscillator.start(start);oscillator.stop(start+0.2);
 }
}
async function unlock(){
 try{
  const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)return;
  if(!context){context=new Audio();output=context.createGain();output.gain.value=ringing?1:0;output.connect(context.destination);}
  if(context.state!=='running')await context.resume();
  if(ringing&&!notes.size)phrase();
 }catch{/* Browsers require a user gesture before they can play incoming audio. */}
}
export function stopIncomingRingtone({silence=true}={}){
 if(silence&&ringing)silenced.add(ringing);
 ringing=null;clearInterval(timer);clearTimeout(deadline);timer=null;deadline=null;
 if(output)output.gain.setValueAtTime(0,context.currentTime);
 for(const oscillator of notes){try{oscillator.stop();}catch{}}
 notes.clear();
}
export function setIncomingRingtone(id){
 if(!id){stopIncomingRingtone({silence:false});return;}
 if(id===ringing||silenced.has(id))return;
 stopIncomingRingtone({silence:false});ringing=id;
 if(silenced.size>128)silenced.clear();
 if(output)output.gain.setValueAtTime(1,context.currentTime);
 phrase();timer=setInterval(phrase,3000);
 deadline=setTimeout(()=>stopIncomingRingtone(),90000);
}
document.addEventListener('pointerdown',unlock,{capture:true});
document.addEventListener('keydown',unlock,{capture:true});
window.addEventListener('pagehide',()=>stopIncomingRingtone());
window.addEventListener('offline',()=>stopIncomingRingtone());
