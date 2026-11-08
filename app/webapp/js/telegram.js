const standaloneMode=()=>window.PHOTO_BOSS_CONFIG?.STANDALONE===true;
export function initTelegram(){if(standaloneMode())return null;const tg=window.Telegram?.WebApp;try{tg?.ready();tg?.expand();}catch{}return tg;}
export function applyTelegramTheme(tg,theme){const bg={premium:'#0f1012',light:'#f4f6fa',photo:'#101b23'}[theme];try{if(!standaloneMode()){tg?.setHeaderColor(bg);tg?.setBackgroundColor(bg);if(tg?.isVersionAtLeast?.('7.10'))tg.setBottomBarColor(bg);}}catch{}document.querySelector('meta[name="theme-color"]')?.setAttribute('content',bg);}
export function haptic(tg){if(standaloneMode())return;try{tg?.HapticFeedback?.selectionChanged();}catch{}}
