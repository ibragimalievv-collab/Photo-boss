# Звонки Photo Boss · версия 3.12

В рабочем чате доступны личные аудио- и видеозвонки и общий звонок команды
до 6 участников. Это WebRTC-звонки внутри Mini App, а не встроенные звонки
Telegram и не звонки на телефонные номера.

## Как пользоваться

1. Открыть Photo Boss → Чат → сотрудник или Общий чат.
2. Нажать «Аудио» или «Видео». Подтвердить доступ к устройствам.
3. Получатель видит приглашение в приложении и уведомление от бота.
   «Ответить» позволяет выбрать только звук или звук с камерой.
4. В общем чате можно присоединиться к уже идущему звонку.
5. Микрофон и камера переключаются отдельно. «Завершить» отключает
   участника; создатель группы может завершить звонок для всех.

При сворачивании окна звонка разговор продолжается: кнопка «На связи» в
верхней панели возвращает в него. Держите Mini App открытым: мобильная ОС
может остановить микрофон и фоновые соединения при блокировке экрана.
При отказе в доступе к камере можно повторить вызов в режиме аудио.

## Размещение и ограничения

- Используется существующий Render web service, один экземпляр и один Python
  процесс. Новая подписка для базовых звонков не требуется. Обновление надёжности чата добавляет таблицу подтверждений отправок при старте.
- Комнаты и сигналы существуют только в памяти, исчезают при перезапуске.
  Сигналы доступны только участникам соответствующего вызова. Владелец не
  может незаметно подключаться к личным звонкам через контроль переписки.
- Проверяются подписанный Telegram initData, активность сотрудника, роль,
  принятие действующих правил чата и отдельная сессия устройства.
- Разговоры не записываются. Аудио/видео не проходят через сервер бота.
- Ожидание ответа: 90 секунд; без heartbeat: 60 секунд; максимум одного
  звонка: 2 часа. Для группы нет автоматического приглашения посторонних.
- Нативные уведомления о звонке на заблокированном экране и системный
  интерфейс телефонного вызова не реализованы. Уведомление доставляет бот.
- В обычной сети используются прямые WebRTC-соединения и STUN. Для
  сетей отелей, корпоративного Wi-Fi и мобильных операторов с ограничениями
  нужен TURN. Без него соединение в таких сетях не гарантируется.
- Перед масштабированием Render необходимо перенести signaling в общее
  хранилище. Для групп более 6 участников нужен медиасервер SFU.

## TURN

Сервер поддерживает `CALLS_TURN_URLS` (список адресов через запятую) и
один из способов авторизации:

- `CALLS_TURN_SECRET`: общий секрет coturn REST; клиенту выдаётся временная
  HMAC-учётная запись, исходный секрет остаётся на сервере.
- `CALLS_TURN_USERNAME` и `CALLS_TURN_CREDENTIAL`: учётная запись провайдера.

TURN не создаётся автоматически на Render: его публичный HTTP-порт не
является TURN-сервером. Нужен действующий внешний relay или собственный
coturn с доступными TURN-портами. Не размещать секреты в репозитории.

## Проверка

`tests/test_work_calls.py` проверяет настоящий HTTP API с тестовыми
подписями Telegram: доступ, изоляцию личных звонков, сигналы, группы,
одновременные старты, переподключение, лимиты и истечение сессий.

`tests/work_calls_browser_check.py` запускает три изолированных Chromium
с синтетическими камерой и микрофоном и проверяет входящие RTP-аудиобайты,
декодированные видеокадры, отключение устройств, переключатели и личный
аудиозвонок. Нужен Playwright; `CALLS_CHROMIUM_PATH` задаёт путь к Chromium.
Тест не использует реальные токены, сотрудников или продакшен.

Для полного локального набора тестов используйте изолированную SQLite:
`DATABASE_URL='sqlite+aiosqlite:///:memory:' python -m pytest -q`.

## Reliability update — 2026-09-22

This change extends the existing chat and signaling endpoints, without introducing
another messaging system. Start point: main `2c9ac7a`.

| Requirement | Status before update | Change / evidence |
| --- | --- | --- |
| Dialog list, unread badges, attachments, voice and short video | Implemented | Existing `chat.js`, `recorder.js`; browser checks at 320/390/1200px |
| Reliable sends on a lost response | Missing | IndexedDB pending sends in the existing local database; client UUID; atomic database receipt and payload fingerprint for text and attachments |
| History without gaps during concurrent sends | Partial | Only history responses advance the history cursor; sending cannot skip unseen messages |
| Draft after closing/reloading | Partial, memory only | Text drafts in localStorage, scoped to employee and conversation |
| Send status | Partial, success only | Pending / sending / error / retry; confirmed messages retain the sent indicator (not a read receipt) |
| Owner deletes only own sent messages | Implemented | Unchanged server predicate; tests also prevent resurrection by retry |
| Mute/camera toggles | Partial | Reuse a live track with `enabled`; no new capture request on ordinary off/on |
| Physical camera switch | Implemented with stream replacement | Try changing constraints in place first; fallback requests video only and retains microphone |
| Preview / recipient orientation | Unmirrored in existing implementation | Retain native orientation and no CSS mirroring for either view; separate browser assertions; physical portrait/landscape checks still required |
| Incoming ringtone, accept, reject, finish | Implemented | Added explicit reject inside the incoming dialog; existing ringtone and cleanup checks |
| Reconnect | Partial | Retry signaling with a stable ID, bounded duplicate guard, up to three ICE restarts after disconnection; up to 50 seconds of signaling outage |
| Production TURN and physical phones | Not verified in this turn | Release acceptance remains open |

The additive `work_chat_send_receipts` table is created by the existing
`Base.metadata.create_all` startup path. It uses a composite primary key
(sender, client UUID); reservation and message insert commit in one transaction.
Receipts survive message deletion, contain only a fingerprint and message ID,
and prevent delayed retries from restoring deleted content. Old clients without
UUID remain compatible but do not receive retry deduplication: reload Mini App
after rollout. Existing messages and attachments are not rewritten.

Pending text/files/recordings survive reload when device storage is available.
Draft text is persisted; an unsubmitted file selection is not restored on reload.
Automatic delivery retries occur while Mini App is running and on its next open;
closing it does not run a background uploader. Quota/storage failure keeps the
original compose fields and displays an error. Device storage clearing removes
local drafts and unsent data. Rejected permissions/authorization are not blindly
retried. A sent indicator confirms server persistence, not recipient reading.

### Camera/microphone permission behavior

No application confirmation is added for each call. A muted track is reused
within a call; switching cameras first tries `applyConstraints`, then falls back
to video-only capture if the host cannot switch the existing source. On hangup,
all tracks stop. Across calls, Telegram/WebView and OS control whether an earlier
grant is retained; Mini App cannot promise a permanent grant or override denial.
The camera is not kept capturing between calls merely to suppress prompts.
Reference: https://developer.mozilla.org/en-US/docs/Web/API/MediaStreamTrack/enabled
and https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia .

### Foreground, background, closed

| State | Supported by implementation | Limits |
| --- | --- | --- |
| Open Mini App | Poll incoming calls, show banner and incoming dialog, play ringtone after audio is unlocked by a user gesture | Sound also depends on device settings and autoplay policy |
| Call panel minimized inside Mini App | Call continues and can be reopened | Mini App must keep executing |
| Telegram in background / phone locked | Best-effort Telegram bot notification; current connection may survive temporarily | No guaranteed background WebRTC or continuous ringtone; OS may suspend it |
| Mini App closed / page unloaded | Existing bot notification only; pagehide releases devices | No native incoming-call screen, automatic media pickup, or CallKit/Android telecom integration |

### TURN budget proposal, not activated

Cloudflare Realtime TURN lists $0.05 per outbound real-time GB. At that listed
rate, 100 billable GB costs $5, 200 GB costs $10; these are usage examples, not
a fixed subscription or an enforced cap. Source checked 2026-09-22:
https://developers.cloudflare.com/realtime/turn/ .

Proposed pilot budget: up to $10/month, subject to owner approval and actual
connectivity testing from operating networks. No account purchase or paid
activation has been performed. Production credentials/configuration have not
been inspected: the Render connector requires confirmation of `My Workspace`
before selecting it. Configured TURN URLs alone do not prove relay reachability.

### Physical acceptance (all pending; do not mark release accepted)

Use two authorized test employees and record phone model, OS/Telegram version,
network/operator, timestamp, and the tested commit. Run each direction:

1. Android on Wi-Fi ↔ iPhone on mobile network, then exchange networks.
2. Ring, accept audio/video, reject from both banner and incoming dialog, cancel,
   hang up on either side; verify camera/mic indicators clear.
3. Grant once and repeat calls, mute/unmute, camera off/on, front/back. Record
   exactly which host prompt recurs; do not infer permission retention from desktop.
4. Deny mic, deny camera, revoke granted permission; verify readable error and
   audio-only recovery after camera denial, no silent retry loop.
5. Show a card with readable text and an arrow on each camera. Turn each phone
   portrait/landscape; inspect local preview and remote image separately.
6. Interrupt signaling/network for 10–20 seconds, restore/change network, verify
   media resumes and messages arrive exactly once. Try loss after server commit.
7. Force relay-only on an isolated approved test session; inspect nominated
   candidate pair `candidateType=relay` and increasing RTP bytes/decoded frames
   on both phones. Test UDP and TLS/TCP 443 as supported by the provider.
8. Background, lock, close, reopen. Record actual notification delivery and call
   behavior; Telegram bot notifications depend on user notification settings.

Synthetic browser tests and an isolated local relay are not substitutes for
Android ↔ iPhone / Wi-Fi ↔ cellular / production TURN evidence.

### Evidence from this execution

- Full Python suite: **300 passed, 1 failed**. The failure is
  `MiniAppTests.test_access_screen_shows_only_signed_own_identity`: an assertion
  expects a two-hour-old signed token to expire, while current main uses a
  24-hour Mini App window. Reproduced on an unchanged `2c9ac7a` worktree; this
  change does not alter authorization. Resolve that existing gate separately.
- Nine chat browser scenarios passed: roles OWNER/ADMIN/PHOTOGRAPHER/MANAGER,
  320/390/1200px, drafts after reload, lost acknowledgement replay with the same
  client UUID, attachments, deletion, unread badges and injection escaping.
- Full WebRTC RTP browser test remains **blocked locally**, reproduced using
  unchanged main call JavaScript. Minimal native Chromium probe with no Photo
  Boss code, no STUN and one audio transceiver returned zero ICE candidates
  while gathering. This runtime result cannot establish production connectivity.
- CI retains the full three-client RTP and relay-only tests; a separate controls
  scenario tests grant reuse and denial before attempting the RTP gate.
- Android/iPhone devices and production TURN credentials were not available to
  this execution. No claim of completed physical acceptance or production release.
- Separate browser controls run: **passed** camera off/on without extra capture
  acquisition, microphone toggle, front/back fallback, denied audio/video
  permission errors, recovery after denial, and release of every track on hangup.
