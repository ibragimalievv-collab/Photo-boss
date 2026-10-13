"""Generate Russian speech for a controlled HR-voice-agent preview.

Produces audio bytes only. SIP provider playback is a separate integration.
"""
from __future__ import annotations

import os

import aiohttp

ENDPOINT = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
VOICES = frozenset({"filipp", "jane"})
MAX_AUDIO_BYTES = 5 * 1024 * 1024


class YandexSpeechSynthesizer:
    def __init__(self, *, api_key: str, voice: str = "filipp"):
        if not api_key.strip():
            raise ValueError("Yandex SpeechKit key missing")
        if voice not in VOICES:
            raise ValueError("Unsupported SpeechKit voice")
        self._key = api_key
        self._voice = voice

    @classmethod
    def from_env(cls) -> "YandexSpeechSynthesizer":
        return cls(
            api_key=os.getenv("PB_HR_SPEECHKIT_API_KEY", ""),
            voice=os.getenv("PB_HR_SPEECHKIT_VOICE", "filipp"),
        )

    async def synthesize(self, text: str) -> bytes:
        if not text or len(text) > 900:
            raise ValueError("Only short spoken responses are supported")
        timeout = aiohttp.ClientTimeout(total=25, connect=8)
        headers = {"Authorization": f"Api-Key {self._key}"}
        data = {
            "text": text, "lang": "ru-RU",
            "voice": self._voice, "format": "oggopus",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(ENDPOINT, headers=headers, data=data) as response:
                    if response.status != 200:
                        raise RuntimeError(f"SpeechKit TTS unavailable (HTTP {response.status})")
                    if int(response.headers.get("Content-Length", "0")) > MAX_AUDIO_BYTES:
                        raise RuntimeError("Oversized TTS audio")
                    result = await response.content.read(MAX_AUDIO_BYTES + 1)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RuntimeError("SpeechKit TTS failed") from exc
        if not result or len(result) > MAX_AUDIO_BYTES:
            raise RuntimeError("No usable TTS audio")
        return result
