export function initTelegram(){const tg=window.Telegram?.WebApp;try{tg?.ready();tg?.expand();}catch{}return tg;}
export function applyTelegramTheme(tg,theme){const bg={premium:'#101215',light:'#f4f6fa',photo:'#101b23'}[theme];try{tg?.setHeaderColor(bg);tg?.setBackgroundColor(bg);if(tg?.isVersionAtLeast?.('7.10'))tg.setBottomBarColor(bg);}catch{}document.querySelector('meta[name="theme-color"]')?.setAttribute('content',bg);}
export function haptic(tg){try{tg?.HapticFeedback?.selectionChanged();}catch{}}
export function openBot(tg,url){if(!/^https:\/\/t\.me\/[a-zA-Z0-9_]+(?:\?start=[a-zA-Z0-9_-]+)?$/.test(url))throw new Error('Некорректная ссылка бота');if(tg?.openTelegramLink)tg.openTelegramLink(url);else window.open(url,'_blank','noopener,noreferrer');}
