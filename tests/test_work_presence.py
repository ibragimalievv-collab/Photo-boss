import pytest

from app.miniapp_security import AccessError
from app.work_presence import MAX_SESSIONS, PRESENCE_TTL, Presence


def test_multiple_devices_delayed_heartbeats_and_disconnect_expiry():
    now = 1000
    state = Presence(clock=lambda: now)
    first = {"sessionId": "first-device-123456789", "sequence": 1, "online": True}
    second = {**first, "sessionId": "second-device-12345678"}
    state.update(3, first)
    state.update(3, second)
    state.update(3, {**first, "sequence": 3, "online": False})
    state.update(3, {**first, "sequence": 2})  # Arrives after the offline beacon.
    assert state.online() == {3}  # The second device is still visible.
    now += PRESENCE_TTL - 1
    assert state.online() == {3}
    now += 1
    assert state.online() == set()  # Lost connection; no unload event required.
    state.update(3, {**second, "sequence": 2})
    assert state.online() == {3}
    state.update(3, {**second, "sequence": 3, "online": False})
    assert state.online() == set()
    now += PRESENCE_TTL * 4
    state.prune()
    assert state.sessions == {}


def test_presence_payload_and_session_limits():
    state = Presence()
    good = {"sessionId": "fixture-device-1234567", "sequence": 1, "online": True}
    for changes in ({"sequence": True}, {"sequence": -1}, {"online": "false"},
                    {"sessionId": "<script>"}, {"userId": 2}):
        with pytest.raises(AccessError):
            state.update(3, {**good, **changes})
    for index in range(MAX_SESSIONS):
        state.update(3, {**good, "sessionId": f"fixture-device-{index:020d}"})
    with pytest.raises(AccessError) as exc:
        state.update(3, good)
    assert exc.value.status == 429
