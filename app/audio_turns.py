"""Bounded PCM endpointing for an explicitly selected Yandex voice transport.

This detects energy and pauses, not words or intent. Browser microphone capture,
echo cancellation and authority checks remain in their existing owners.
"""
import math
import struct
from collections import deque


class AudioTurns:
    RATE = 44100
    FRAME_SAMPLES = RATE // 50
    FRAME_BYTES = FRAME_SAMPLES * 2
    START_FRAMES = 6             # 120 ms, ignoring isolated clicks.
    END_FRAMES = 40              # 800 ms; retain short pauses inside a phrase.
    PRE_ROLL_FRAMES = 12         # Preserve the beginning of a detected word.
    MAX_TURN_FRAMES = 90 * 50
    MAX_TURNS = 38               # The app limits one conversation to 78 items.

    def __init__(self):
        self.partial = bytearray()
        self.pre_roll = deque(maxlen=self.PRE_ROLL_FRAMES)
        self.speaking = False
        self.voiced = self.quiet = self.turn_frames = self.turns = 0
        self.noise = 0.0004
        self.last_packet_at = None
        self.closed = False

    def feed(self, raw, now):
        if self.closed or not raw or len(raw) % 2 or len(raw) > 32768:
            raise ValueError('invalid_audio_turn_frame')
        self.last_packet_at = now
        self.partial.extend(raw)
        actions = []
        while len(self.partial) >= self.FRAME_BYTES:
            frame = bytes(self.partial[:self.FRAME_BYTES])
            del self.partial[:self.FRAME_BYTES]
            samples = struct.unpack('<' + 'h' * self.FRAME_SAMPLES, frame)
            energy = math.sqrt(sum(x*x for x in samples) / self.FRAME_SAMPLES) / 32768
            threshold = max(0.003, min(0.02, self.noise * 4))
            loud = energy >= threshold
            if not self.speaking:
                self.pre_roll.append(frame)
                self.voiced = self.voiced + 1 if loud else 0
                if not loud:
                    self.noise = self.noise * 0.98 + energy * 0.02
                if self.voiced < self.START_FRAMES:
                    continue
                if self.turns >= self.MAX_TURNS:
                    raise ValueError('audio_turn_limit')
                self.speaking = True
                self.turn_frames = len(self.pre_roll)
                self.quiet = 0
                actions.append(('speech_started', None))
                actions.extend(('audio', chunk) for chunk in self.pre_roll)
                self.pre_roll.clear()
                continue
            self.turn_frames += 1
            if self.turn_frames > self.MAX_TURN_FRAMES:
                raise ValueError('audio_turn_duration_limit')
            actions.append(('audio', frame))
            self.quiet = 0 if loud else self.quiet + 1
            if self.quiet >= self.END_FRAMES:
                actions.extend(self._finish())
        return actions

    def poll(self, now):
        # A muted/stalled microphone may stop sending PCM instead of silence.
        # The bridge serializes this with feed; no further audio is invented.
        if not self.closed and self.speaking and self.last_packet_at is not None and now - self.last_packet_at >= 0.85:
            tail = [('audio', bytes(self.partial))] if self.partial else []
            self.partial.clear()
            return tail + self._finish()
        return []

    def _finish(self):
        self.speaking = False
        self.voiced = self.quiet = self.turn_frames = 0
        self.turns += 1
        self.pre_roll.clear()
        return [('speech_stopped', None), ('commit', None), ('response', None)]

    def close(self):
        self.closed = True
        self.partial.clear()
        self.pre_roll.clear()
        self.speaking = False
