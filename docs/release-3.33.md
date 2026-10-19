# Photo Boss 3.33

The owner confirmed adding hotel-zone checks and protection against arbitrary attendance photos. This release adds configurable hotel reference points and a manual-review mode; it does not claim automated face, uniform or liveness recognition.

In production, `PHOTO_BOSS_ATTENDANCE_REQUIRE_REVIEW=1` makes online arrivals and departures save private photo evidence, device coordinates, capture receipt time and hotel-zone classification as `PENDING_REVIEW`. It does not start/finish attendance or create an automatic fine. A different owner or scoped administrator reviews the person, uniform, location and verified time in More → Team and HR → Attendance awaiting review. Existing late-fee rules apply only after explicit approval using the verified time. Self-approval is refused. The old Telegram photo path cannot bypass protected mode; it directs staff to the app. Offline capture remains pending and is explicitly identified as device-claimed time.

The hotel comes from the day's shift plan, or a single active membership when no plan exists. Multiple hotels never silently resolve to the nearest point. Review scope still requires the day's plan for administrators; unplanned/shared days remain owner-only. An immutable per-capture context retains the point revision, distance, reported accuracy, radius and classification. Later point edits cannot change old evidence.

Inside means reported distance + accuracy is within radius; outside means distance - accuracy is beyond it; crossing the boundary is uncertain. Missing, zero or poor accuracy is always uncertain. The device can falsify GPS, so even an inside result requires review. No location is collected continuously.

More → Hotel points and zones lets owners and scoped administrators edit address, latitude, longitude and 20–1000 m radius. Concurrent edits use a revision check; all edits are audited. The Movenpick seed binds only one active hotel matching Movenpick/Мёвенпик aliases and fails closed on ambiguity. It updates that hotel's address, without creating or altering another hotel.

Production seed: Pionersky prospect 253/1, Anapa; latitude 44.979340, longitude 37.265541, initial radius 200 m. This is an initial circle around the verified map point, not a surveyed boundary. Adjust the circle after checking the actual workplace and device accuracy. A stable seed request ID prevents overwriting subsequent edits. Remove `PHOTO_BOSS_SALES_FINES_RESET_REQUEST` after the completed 3.32 reset; its immutable archive remains retained.

Still required for automatic identity/uniform verification: employee reference photos, approved uniform examples and a separately validated identity/liveness service. This release uses live camera in the normal UI and fully decodes JPEG on the server, but it does not treat the client camera path as tamper-proof evidence.
