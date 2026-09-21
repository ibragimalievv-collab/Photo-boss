// Request the physical camera directly: neither previews nor recordings are mirrored.
export function cameraConstraints(facing='user',exact=false){
 return {width:{ideal:640},height:{ideal:480},frameRate:{ideal:20,max:24},facingMode:exact?{exact:facing}:{ideal:facing}};
}
export async function openCamera(facing='user',exact=false){
 const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:cameraConstraints(facing,exact)});
 const actual=stream.getVideoTracks()[0]?.getSettings?.().facingMode;
 if(exact&&actual&&actual!==facing){stream.getTracks().forEach(t=>t.stop());throw new Error('Эта камера недоступна на устройстве.');}
 return stream;
}
