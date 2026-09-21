import {esc} from '/app/js/domain.js';

const paths={
 flip:'M20 7V3l-3 3M4 17v4l3-3M20 7a9 9 0 0 0-15-2M4 17a9 9 0 0 0 15 2M8 12a4 4 0 1 1 8 0 4 4 0 0 1-8 0',
 back:'m15 18-6-6 6-6',close:'m6 6 12 12M6 18 18 6',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
 chat:'M21 11.5a9 9 0 0 1-9 9H4l-3 2 1.5-6A9 9 0 1 1 21 11.5Z',
 group:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
 phone:'M22 16.9v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.2 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.69 2.79a2 2 0 0 1-.45 2.11L8.08 9.89a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.89.33 1.83.56 2.79.69A2 2 0 0 1 22 16.9Z',
 video:'m23 7-7 5 7 5ZM3 5h11a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z',
 mic:'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3ZM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8',
 attach:'m21.4 11.6-9.2 9.2a6 6 0 0 1-8.5-8.5L13.6 2.4a4 4 0 0 1 5.7 5.7L9.4 18a2 2 0 0 1-2.8-2.8l9.2-9.2',
 send:'m22 2-7 20-4-9L2 9Zm0 0L11 13',check:'m5 12 4 4L19 6',down:'m6 9 6 6 6-6',
 play:'m8 5 11 7-11 7Z',file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8ZM14 2v6h6M8 13h8M8 17h5',
 volume:'m11 5-6 4H2v6h3l6 4ZM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14',
 hangup:'M3 16H1v-5c6-6 16-6 22 0v5h-5v-4a16 16 0 0 0-12 0v4Z',
 minimize:'m7 2 0 5-5 0M17 22v-5h5M7 7 1 1M17 17l6 6',stop:'M6 6h12v12H6Z',shield:'m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6ZM8 12l3 3 5-6'
};
export function icon(name){return `<svg class="pb-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.chat}"/></svg>`;}
export function initials(name){return String(name).trim().split(/\s+/).slice(0,2).map(p=>p[0]||'').join('').toUpperCase();}
export function avatar(name,id=0,group=false){return `<span class="pb-chat-avatar tone-${Math.abs(Number(id)||0)%5}" aria-hidden="true">${group?icon('group'):esc(initials(name))}</span>`;}
