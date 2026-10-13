"""Yandex AI Studio chat completion adapter for the isolated HR dialogue engine.

Uses the documented Yandex OpenAI-compatible Chat Completions REST endpoint.
This class does not initiate calls, persist answers or mutate HR records.
"""
from __future__ import annotations

import os
import re

import aiohttp

ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"
FOLDER_RE = re.compile(r"^[a-zA-Z0-9_-]{5,100}$")
MODEL_RE = re.compile(r"^[a-zA-Z0-9._/-]{2,100}$")


class ModelConfigurationError(ValueError):
    pass


def build_payload(
    folder_id: str,
    model: str,
    messages: list[dict[str, str]],
) -> dict:
    if not FOLDER_RE.fullmatch(folder_id):
        raise ModelConfigurationError("Invalid Yandex folder ID")
    if not MODEL_RE.fullmatch(model) or model.startswith("/"):
        raise ModelConfigurationError("Invalid Yandex model name")
    if not messages or len(messages) > 31:
        raise ValueError("Expected 1 to 31 messages")
    if any(row.get("role") not in {"system", "user", "assistant"} or
           not isinstance(row.get("content"), str) for row in messages):
        raise ValueError("Unsupported chat message")
    return {
        "model": f"gpt://{folder_id}/{model}",
        "messages": messages,
        "max_tokens": 240,
        "temperature": 0.45,
        "stream": False,
    }


def parse_completion(response: dict) -> str:
    try:
        value = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Unexpected Yandex AI reply") from None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Empty Yandex AI reply")
    return value.strip()


class YandexChatModel:
    def __init__(self, *, api_key: str, folder_id: str, model: str = "yandexgpt/latest"):
        if not api_key.strip():
            raise ModelConfigurationError("Yandex API key is missing")
        build_payload(folder_id, model, [{"role": "user", "content": "проверка"}])
        self._api_key = api_key
        self._folder_id = folder_id
        self._model = model

    @classmethod
    def from_env(cls) -> "YandexChatModel":
        # Keep these settings separate from main Photo Boss and OPENAI_API_KEY.
        return cls(
            api_key=os.getenv("PB_HR_AI_API_KEY", ""),
            folder_id=os.getenv("PB_HR_AI_FOLDER_ID", ""),
            model=os.getenv("PB_HR_AI_MODEL", "yandexgpt/latest"),
        )

    async def answer(self, messages: list[dict[str, str]]) -> str:
        payload = build_payload(self._folder_id, self._model, messages)
        headers = {
            "Authorization": f"Api-Key {self._api_key}",
            "OpenAI-Project": self._folder_id,
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=22, connect=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(ENDPOINT, headers=headers, json=payload) as response:
                    if response.status != 200:
                        # Never expose upstream bodies: some contain request details.
                        raise RuntimeError(f"Yandex AI unavailable (HTTP {response.status})")
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RuntimeError("Yandex AI request failed") from exc
        return parse_completion(data)
