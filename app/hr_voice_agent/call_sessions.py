"""Consent-gated in-memory call sessions.

A verified telephony adapter can connect here later. No outbound dial is made.
Sessions do not survive process restarts; no transcripts or candidate phones logged.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .brain import AgentTurn, HRVoiceAgent, InterviewState, may_call_candidate, normalized_russian_mobile


@dataclass
class CallSession:
    phone: str
    state: InterviewState
    started_at: float
    lock: asyncio.Lock


class CallSessionController:
    MAX_SESSIONS = 20
    MAX_DURATION_SECONDS = 20 * 60

    def __init__(self, agent: HRVoiceAgent):
        self.agent = agent
        self._calls: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, *, call_id: str, phone: str, candidate_ref: str,
                    explicit_call_consent: bool) -> AgentTurn:
        """Only called after a provider reports a connected opted-in outbound call."""
        if not call_id or len(call_id) > 120 or not candidate_ref:
            raise ValueError("Invalid call or candidate reference")
        if not may_call_candidate(phone, explicit_call_consent=explicit_call_consent):
            raise PermissionError("Explicit call consent and valid mobile number required")
        async with self._lock:
            if call_id in self._calls:
                raise ValueError("Call already exists")
            self._prune()
            if len(self._calls) >= self.MAX_SESSIONS:
                raise RuntimeError("Maximum simultaneous calls reached")
            self._calls[call_id] = CallSession(
                phone=normalized_russian_mobile(phone) or "",
                state=InterviewState(candidate_ref=candidate_ref),
                started_at=time.monotonic(),
                lock=asyncio.Lock(),
            )
        return self.agent.opening()

    async def final_utterance(self, *, call_id: str, text: str) -> AgentTurn:
        session = self._calls.get(call_id)
        if session is None:
            raise KeyError("Unknown or expired call")
        async with session.lock:
            if time.monotonic() - session.started_at >= self.MAX_DURATION_SECONDS:
                await self.finish(call_id)
                return AgentTurn("Лимит времени разговора достигнут. Всего доброго!", "ended")
            turn = await self.agent.respond(session.state, text)
            if turn.status in {"ended", "handed_off"}:
                await self.finish(call_id)
            return turn

    async def finish(self, call_id: str) -> InterviewState | None:
        """Forget the live session on hangup. Persisting any summary is external."""
        async with self._lock:
            session = self._calls.pop(call_id, None)
        return session.state if session else None

    def _prune(self) -> None:
        now = time.monotonic()
        for cid, session in list(self._calls.items()):
            if now - session.started_at >= self.MAX_DURATION_SECONDS:
                self._calls.pop(cid, None)
