"""Ephemeral presence for the single-process Mini App, across tabs/devices."""
import re
import time

from .miniapp_security import AccessError

PRESENCE_TTL = 45
MAX_SESSIONS = 16


class Presence:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.sessions = {}

    def prune(self):
        now = self.clock()
        # Retain offline sequence numbers so a delayed heartbeat cannot revive a tab.
        self.sessions = {key: value for key, value in self.sessions.items()
                         if now - value[0] < PRESENCE_TTL * 4}

    def update(self, user_id, body):
        if set(body) != {"sessionId", "sequence", "online"}:
            raise AccessError("Некорректные данные присутствия.", 400)
        sid, sequence, online = body["sessionId"], body["sequence"], body["online"]
        if (not isinstance(sid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{20,64}", sid)
                or type(sequence) is not int or not 0 < sequence < 2**53
                or type(online) is not bool):
            raise AccessError("Некорректные данные присутствия.", 400)
        self.prune()
        key = (user_id, sid)
        old = self.sessions.get(key)
        if old and sequence <= old[1]:
            return
        if not old and sum(uid == user_id for uid, _ in self.sessions) >= MAX_SESSIONS:
            raise AccessError("Слишком много окон приложения. Повторите позже.", 429)
        self.sessions[key] = (self.clock(), sequence, online)

    def online(self):
        self.prune()
        now = self.clock()
        return {uid for (uid, _), (seen, _, active) in self.sessions.items()
                if active and now - seen < PRESENCE_TTL}
