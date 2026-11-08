// Microphone samples stay in this site's AudioContext; output is always silent.
class PhotoBossPCM extends AudioWorkletProcessor {
 constructor(){super();this.inputCount=0;this.next=0;this.previous=0;this.buffer=[];}
 process(inputs,outputs){
  for(const output of outputs)for(const channel of output)channel.fill(0);
  const source=inputs[0]?.[0];if(!source)return true;
  const step=sampleRate/44100;
  for(const value of source){
   const index=this.inputCount++;
   while(this.next<=index){
    const fraction=Math.max(0,Math.min(1,this.next-(index-1)));
    this.buffer.push(this.previous+(value-this.previous)*fraction);this.next+=step;
    if(this.buffer.length>=882){
     const pcm=new ArrayBuffer(this.buffer.length*2),view=new DataView(pcm);
     for(let i=0;i<this.buffer.length;i++)view.setInt16(i*2,Math.round(Math.max(-1,Math.min(1,this.buffer[i]))*32767),true);
     this.port.postMessage(pcm,[pcm]);this.buffer=[];
    }
   }
   this.previous=value;
  }
  return true;
 }
}
registerProcessor('photo-boss-pcm',PhotoBossPCM);
