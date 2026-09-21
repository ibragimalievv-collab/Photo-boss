import {api, ApiError} from '/app/js/api.js';
import {esc} from '/app/js/domain.js';
import {setIncomingRingtone,stopIncomingRingtone} from '/work-chat/ringtone.js';

// A call has its own dialog so chat navigation cannot accidentally capture media
// or destroy an active connection. Only a deliberate Join/Call click gets media.
const panel = document.createElement('dialog');
panel.className = 'pb-call-dialog';
panel.setAttribute('aria-labelledby', 'pbCallTitle');
document.body.append(panel);
const css = document.createElement('link');
css.rel = 'stylesheet'; css.href = '/work-chat/calls.css'; document.head.append(css);
const banner = document.createElement('div');
banner.className = 'pb-call-banner'; banner.hidden = true;
banner.setAttribute('role', 'status'); document.body.append(banner);

let me, active = null, available = [], selected = null, listTimer, syncTimer;
let starting = false, polling = false, controlsBusy = false, lastFocus;
const ignored = new Set();
const callApi = (path, body) => api(`/chat/calls${path}`, body === undefined ? {} : {method:'POST', body});
const status = text => { const e = panel.querySelector('[data-call-status]'); if(e) e.textContent = text; };
const mediaMessage = e => ({NotAllowedError:'Разрешите микрофон и камеру в настройках Telegram или браузера и повторите звонок.',
    NotFoundError:'Микрофон или камера не найдены. Проверьте устройство.',
    NotReadableError:'Микрофон или камера заняты другим приложением.',
    OverconstrainedError:'Камера не поддерживает выбранный режим.'}[e.name] || e.message || 'Не удалось начать звонок.');

function title(room) {
    if(room.group) return 'Групповой звонок';
    return room.participants.find(p => p.id !== me?.user?.id)?.name ||
        (room.creatorId !== me?.user?.id ? room.creatorName : selected?.title) || 'Личный звонок';
}
function showPanel(html) {
    panel.innerHTML = html;
    if(!panel.open) { lastFocus = document.activeElement; panel.showModal(); }
}
function fail(message) {
    showPanel(`<h2 id="pbCallTitle">Звонок Photo Boss</h2><p role="alert">${esc(message)}</p><button data-call-close>Закрыть</button>`);
}
function setBanner() {
    const parent=document.querySelector('.pb-chat-dialog[open]') || document.body;
    if(banner.parentNode!==parent) parent.append(banner);
    const top=document.querySelector('#topbar');
    let resume=top?.querySelector('[data-top-call]');
    if(active && top && !resume) {resume=document.createElement('button');resume.className='pb-chat-trigger pb-call-active';resume.dataset.topCall='1';resume.textContent='☎ На связи';resume.onclick=()=>{if(active&&!panel.open)panel.showModal();};top.append(resume);}
    if(!active) resume?.remove();
    // Do not replace an incoming call's accept control on every background poll.
    const room = !active && available.find(r => !ignored.has(r.id) && r.creatorId !== me?.user?.id && !r.participants.some(p => p.id === me?.user?.id));
    setIncomingRingtone(room?.id || null);
    if(!room) {banner.hidden = true; banner.dataset.room = ''; return;}
    if(banner.dataset.room === room.id) return;
    banner.dataset.room = room.id; banner.hidden = false;
    banner.innerHTML = `<strong>${esc(room.creatorName)} · ${room.group ? 'Групповой звонок' : room.mode === 'video' ? 'Видеозвонок' : 'Аудиозвонок'}</strong><div><button data-incoming="${esc(room.id)}">Ответить</button><button data-ignore="${esc(room.id)}">${room.group ? 'Скрыть' : 'Отклонить'}</button></div>`;
}
async function refresh() {
    if(!me) return;
    try {
        const data = await callApi(''); available = data.calls; setBanner();
        for(const id of ignored) if(!available.some(r => r.id === id)) ignored.delete(id);
        document.dispatchEvent(new CustomEvent('pb-calls-updated'));
    } catch { /* The existing chat displays authentication and rules errors. */ }
}
export function initCalls(user) {
    me = user;
    if(listTimer) return;
    refresh(); listTimer = setInterval(refresh, 5000);
}
export function callToolbar(peer, name) {
    selected = {peer, title:name};
    const room = available.find(r => peer === null ? r.group : !r.group &&
        [r.creatorId,r.peerId].includes(peer) && [r.creatorId,r.peerId].includes(me?.user?.id));
    return `<div class="pb-call-toolbar"><button data-start-call="audio">☎ Аудио</button><button data-start-call="video">▣ Видео</button>${room ? `<button data-join-call="${esc(room.id)}">Присоединиться (${room.participants.length}/6)</button>` : ''}${active ? '<button data-resume-call>Текущий звонок</button>' : ''}<small>${peer === null ? 'До 6 участников' : 'Личный звонок'}</small></div>`;
}

async function media(mode) {
    if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection)
        throw new ApiError('Звонки недоступны в этой версии Telegram. Обновите Telegram и откройте Photo Boss заново.');
    return navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true},
        video:mode === 'video' ? {width:{ideal:640},height:{ideal:360},frameRate:{ideal:20,max:24},facingMode:'user'} : false});
}
function liveUI(c) {
    showPanel(`<header><div><small>PHOTO BOSS · ЗВОНОК</small><h2 id="pbCallTitle">${esc(title(c.room))}</h2></div><button data-minimize-call aria-label="Свернуть звонок">−</button></header>
        <p data-call-status role="status">Ожидаем участников…</p><div class="pb-call-grid" data-call-grid></div>
        <div class="pb-call-controls"><button data-mic aria-pressed="true">Микрофон вкл.</button><button data-camera aria-pressed="${c.video}">${c.video ? 'Камера вкл.' : 'Камера выкл.'}</button><button data-play-audio>Включить звук</button><button class="danger" data-hangup>Завершить</button></div>
        ${c.room.group && c.room.creatorId === me.user.id ? '<button class="pb-call-end-all" data-end-all>Завершить для всех</button>' : ''}
        <p class="pb-call-note">Разговор не записывается. Держите приложение открытым. ${!c.relayConfigured ? 'В некоторых сетях соединение может быть недоступно.' : ''}</p>`);
    tile(me.user.id, 'Вы', c.stream, true);
}
function tile(id, name, stream, local = false) {
    const grid = panel.querySelector('[data-call-grid]'); if(!grid) return;
    let card = grid.querySelector(`[data-participant="${id}"]`);
    if(!card) {
        card = document.createElement('div'); card.className='pb-call-tile'; card.dataset.participant=id;
        card.innerHTML=`<video autoplay playsinline></video><span class="pb-call-avatar">${esc(name.slice(0,2).toUpperCase())}</span><div><strong>${esc(name)}</strong><small data-peer-state>Подключение…</small></div>`;
        grid.append(card);
    }
    const video = card.querySelector('video'); video.muted = local;
    if(video.srcObject !== stream) {video.srcObject=stream; video.play().catch(()=>{});}
    return card;
}
async function begin({peer=null, room=null, mode='audio', name=''} = {}) {
    if(starting) return;
    if(active) {panel.showModal(); return;}
    starting=true; let stream=null, result=null;
    stopIncomingRingtone();
    try {
        if(!room) selected={peer,title:name};
        stream=await media(mode);
        result=await callApi(room ? '/join' : '/start', room ? {callId:room.id,mode} : {peerId:peer,mode});
        const c={room:result.call,session:result.session,stream,peers:new Map(),cursor:0,
            audio:true,video:mode==='video',iceServers:result.iceServers,relayConfigured:result.relayConfigured,
            lastSync:Date.now(),closed:false};
        active=c; banner.hidden=true; liveUI(c);setBanner();
        window.Telegram?.WebApp?.enableClosingConfirmation?.();
        syncTimer=setInterval(sync,1000); await sync(); refresh();
    } catch(e) {
        stream?.getTracks().forEach(t=>t.stop());
        if(active) await hangup(false,mediaMessage(e)); else fail(mediaMessage(e));
    } finally {starting=false;}
}
function send(c, peer, data) {
    peer.sendChain = peer.sendChain.then(async () => {
        if(c.closed || active !== c || c.peers.get(peer.id) !== peer) return;
        await callApi('/signal',{callId:c.room.id,session:c.session,to:peer.id,toSession:peer.session,data});
    }).catch(e=>{if(!c.closed && e.status !== 409) status(e.message);});
    return peer.sendChain;
}
async function offer(c,p,restart=false) {
    if(c.closed || p.offering || p.pc.signalingState !== 'stable') return;
    p.offering=true;
    try {
        const desc=await p.pc.createOffer({iceRestart:restart}); await p.pc.setLocalDescription(desc);
        await send(c,p,{type:'offer',sdp:p.pc.localDescription.sdp});
    } finally {p.offering=false;}
}
async function ensurePeer(c,member) {
    let p=c.peers.get(member.id);
    if(p?.session === member.session) return p;
    if(p) {p.pc.close(); c.peers.delete(member.id);}
    const pc=new RTCPeerConnection({iceServers:c.iceServers});
    p={id:member.id,session:member.session,pc,stream:new MediaStream(),candidates:[],sendChain:Promise.resolve(),restarts:0,created:Date.now()};
    c.peers.set(p.id,p);
    const audio=c.stream.getAudioTracks()[0], video=c.stream.getVideoTracks()[0];
    p.audioSender=pc.addTransceiver(audio || 'audio',{direction:'sendrecv',streams:[c.stream]}).sender;
    p.videoSender=pc.addTransceiver(video || 'video',{direction:'sendrecv',streams:[c.stream]}).sender;
    tile(p.id,member.name,p.stream);
    pc.ontrack=e=>{if(!p.stream.getTracks().includes(e.track))p.stream.addTrack(e.track); tile(p.id,member.name,p.stream);};
    pc.onicecandidate=e=>{if(e.candidate) send(c,p,{type:'candidate',candidate:e.candidate.toJSON()});};
    pc.onconnectionstatechange=()=>{
        if(c.closed) return;
        if(pc.connectionState==='failed' && me.user.id < p.id && p.restarts++ < 1)
            offer(c,p,true).catch(e=>status(e.message));
        updateTiles(c);
    };
    if(me.user.id < p.id) await offer(c,p);
    return p;
}
async function receive(c,s) {
    const p=c.peers.get(s.from); if(!p || p.session !== s.session) return;
    const d=s.data;
    if(d.type === 'candidate') {
        if(p.pc.remoteDescription) await p.pc.addIceCandidate(d.candidate); else p.candidates.push(d.candidate);
        return;
    }
    if((d.type==='offer' && me.user.id < p.id) || (d.type==='answer' && p.pc.signalingState!=='have-local-offer')) return;
    await p.pc.setRemoteDescription(d);
    for(const candidate of p.candidates.splice(0)) await p.pc.addIceCandidate(candidate);
    if(d.type==='offer') {
        await p.pc.setLocalDescription(await p.pc.createAnswer());
        await send(c,p,{type:'answer',sdp:p.pc.localDescription.sdp});
    }
}
function updateTiles(c) {
    let connected=0, failed=false;
    for(const member of c.room.participants) {
        const local=member.id===me.user.id, peer=c.peers.get(member.id);
        const card=panel.querySelector(`[data-participant="${member.id}"]`); if(!card) continue;
        const video=local ? c.video : member.video;
        card.classList.toggle('has-video',video);
        const state=local ? 'Вы' : peer?.pc.connectionState;
        const connectedPeer=state==='connected'; if(connectedPeer) connected++;
        const timedOut=peer && !connectedPeer && Date.now()-peer.created>25000;
        if(state==='failed' || timedOut) failed=true;
        card.querySelector('[data-peer-state]').textContent=local ? (c.audio ? 'Микрофон включён' : 'Микрофон выключен') :
            connectedPeer ? (member.audio ? 'На связи' : 'Микрофон выключен') : state==='failed' || timedOut ? 'Нет соединения' : 'Подключение…';
    }
    status(failed ? 'Не удалось соединиться с участником. Попробуйте другую сеть и подключитесь заново.' :
        connected ? `На связи: ${connected+1} из ${c.room.participants.length}` : c.room.participants.length>1 ? 'Соединяем участников…' : 'Ожидаем участников…');
}
async function sync() {
    const c=active; if(!c || c.closed || polling) return;
    polling=true;
    try {
        const d=await callApi('/sync',{callId:c.room.id,session:c.session,after:c.cursor,audio:c.audio,video:c.video});
        if(active!==c || c.closed) return;
        c.room=d.call; c.lastSync=Date.now();
        for(const [id,p] of c.peers) if(!d.call.participants.some(m=>m.id===id && m.session===p.session)) {
            p.pc.close();c.peers.delete(id);panel.querySelector(`[data-participant="${id}"]`)?.remove();
        }
        for(const m of d.call.participants) if(m.id!==me.user.id) await ensurePeer(c,m);
        for(const s of d.signals) {await receive(c,s); c.cursor=s.seq;}
        c.cursor=d.cursor; updateTiles(c);
    } catch(e) {
        if(active!==c || c.closed) return;
        if([401,403,404,409,428].includes(e.status)) await hangup(false,e.status===404 ? 'Звонок завершён.' : e.message);
        else if(Date.now()-c.lastSync>30000) await hangup(false,'Связь прервалась. Подключитесь к звонку заново.');
        else status('Восстанавливаем соединение…');
    } finally {polling=false;}
}
async function hangup(endForAll=false,message='') {
    const c=active; if(!c) return;
    // Release devices immediately, even if the network is down.
    active=null;c.closed=true; clearInterval(syncTimer);
    c.stream.getTracks().forEach(t=>t.stop()); for(const p of c.peers.values())p.pc.close();
    panel.querySelectorAll('video').forEach(v=>{v.pause();v.srcObject=null;});
    window.Telegram?.WebApp?.disableClosingConfirmation?.();
    if(message) fail(message); else {panel.close();lastFocus?.focus?.();}
    try {await callApi('/leave',{callId:c.room.id,session:c.session,endForAll});} catch { /* Server heartbeat removes stale sessions. */ }
    refresh();
}
async function toggleCamera() {
    const c=active; if(!c || controlsBusy)return; controlsBusy=true;
    let fresh=null;
    try {
        if(c.video) {
            await Promise.all([...c.peers.values()].map(p=>p.videoSender.replaceTrack(null)));
            for(const t of c.stream.getVideoTracks()) {t.stop(); c.stream.removeTrack(t);} c.video=false;
        } else {
            fresh=await navigator.mediaDevices.getUserMedia({audio:false,video:{width:{ideal:640},height:{ideal:360},frameRate:{ideal:20,max:24}}});
            if(active!==c || c.closed) {fresh.getTracks().forEach(t=>t.stop());return;}
            const track=fresh.getVideoTracks()[0];
            await Promise.all([...c.peers.values()].map(p=>p.videoSender.replaceTrack(track)));
            c.stream.addTrack(track);c.video=true;
        }
        const button=panel.querySelector('[data-camera]'); if(button){button.textContent=c.video?'Камера вкл.':'Камера выкл.';button.setAttribute('aria-pressed',String(c.video));}
        updateTiles(c); sync();
    } catch(e) {fresh?.getTracks().forEach(t=>t.stop());status(mediaMessage(e));}
    finally {controlsBusy=false;}
}
export async function handleCallClick(e, peer, name) {
    const start=e.target.closest('[data-start-call]');
    if(start) {await begin({peer,name,mode:start.dataset.startCall});return true;}
    const join=e.target.closest('[data-join-call]');
    if(join) {const room=available.find(r=>r.id===join.dataset.joinCall);if(room) invite(room);else refresh();return true;}
    if(e.target.closest('[data-resume-call]')) {if(active&&!panel.open)panel.showModal();return true;}
    return false;
}
function invite(room) {
    if(active) {if(!panel.open) panel.showModal();return;}
    stopIncomingRingtone();
    showPanel(`<h2 id="pbCallTitle">${esc(title(room))}</h2><p>${esc(room.creatorName)} приглашает ${room.group?'команду':'вас'} в звонок.</p><p>Микрофон и камера включатся после вашего выбора.</p><div class="pb-call-controls"><button data-answer="audio" data-room="${esc(room.id)}">Только аудио</button><button data-answer="video" data-room="${esc(room.id)}">С видео</button><button data-call-close>Позже</button></div>`);
}
panel.addEventListener('click',async e=>{
    const answer=e.target.closest('[data-answer]');
    if(answer) {const room=available.find(r=>r.id===answer.dataset.room);if(room) await begin({room,mode:answer.dataset.answer}); else fail('Звонок уже завершён.');return;}
    if(e.target.closest('[data-hangup]')) return hangup();
    if(e.target.closest('[data-end-all]')) return hangup(true);
    if(e.target.closest('[data-camera]')) return toggleCamera();
    if(e.target.closest('[data-mic]') && active) {
        active.audio=!active.audio;active.stream.getAudioTracks().forEach(t=>{t.enabled=active.audio;});
        const b=e.target.closest('[data-mic]');b.textContent=active.audio?'Микрофон вкл.':'Микрофон выкл.';b.setAttribute('aria-pressed',String(active.audio));updateTiles(active);sync();
    }
    if(e.target.closest('[data-play-audio]')) panel.querySelectorAll('video').forEach(v=>v.play().catch(()=>status('Нажмите «Включить звук» ещё раз.')));
    if(e.target.closest('[data-minimize-call],[data-call-close]')) panel.close();
});
panel.addEventListener('cancel',e=>{e.preventDefault();panel.close();});
banner.addEventListener('click',async e=>{
    const answer=e.target.closest('[data-incoming]');
    if(answer){const room=available.find(r=>r.id===answer.dataset.incoming);if(room)invite(room);return;}
    const ignore=e.target.closest('[data-ignore]');if(!ignore)return;
    const room=available.find(r=>r.id===ignore.dataset.ignore);if(!room)return;
    ignored.add(room.id);setBanner();
    if(!room.group) try{await callApi('/decline',{callId:room.id});}catch{} refresh();
});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){refresh();sync();}});
window.addEventListener('pagehide',()=>{if(active)hangup();});
