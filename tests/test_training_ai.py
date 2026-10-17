"""Academy transport failure contracts without live API calls or credentials."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import training_ai as ai
from app.services.training import CATEGORY_BY_SLUG


@pytest.mark.parametrize(("mode", "code", "retryable"), [
    ("quota", "insufficient_quota", False),
    ("timeout", "timeout", True),
    ("invalid", "invalid_output", True),
    ("score_mismatch", "invalid_output", True),
    ("incomplete", "incomplete", True),
    ("refusal", "refusal", False),
])
def test_academy_persists_safe_failure_reason(monkeypatch, mode, code, retryable):
    class Response:
        status = 429 if mode == "quota" else 200

        async def __aenter__(self):
            if mode == "timeout":
                raise TimeoutError()
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            if mode == "quota":
                return {"error": {"code": "insufficient_quota", "message": "PRIVATE"}}
            data = {"score": 85, "decision": "ACCEPT", "critical": False,
                    "duplicate_pairs": [], "reshoot_indexes": [], "strengths": [],
                    "issues": [], "next_action": "Далее", "criteria": {
                        "focus": 18, "light": 13, "composition": 13,
                        "pose": 17, "emotion": 12, "variety": 12}}
            if mode == "score_mismatch":
                data["score"] = 99
            parts = [{"type": "refusal"}] if mode == "refusal" else [{
                "type": "output_text", "text": "not json" if mode == "invalid" else json.dumps(data)}]
            return {"status": "incomplete" if mode == "incomplete" else "completed",
                    "output": [{"type": "message", "content": parts}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(ai.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(ai, "config", SimpleNamespace(openai_api_key="TEST-ONLY", academy_analysis_model="gpt-4.1-mini"))
    images = [b"\xff\xd8\xfftest\xff\xd9"] * 5
    result = asyncio.run(ai.analyze_training_set(CATEGORY_BY_SLUG["woman"], images, images))
    assert result["errorCode"] == code and result["retryable"] is retryable
    assert "PRIVATE" not in json.dumps(result)


def test_corrupt_academy_image_returns_failure_instead_of_crashing_queue(monkeypatch):
    monkeypatch.setattr(ai, "config", SimpleNamespace(openai_api_key="TEST-ONLY", academy_analysis_model="gpt-4.1-mini"))
    images = [b"not jpeg"] * 5
    result = asyncio.run(ai.analyze_training_set(CATEGORY_BY_SLUG["woman"], images, images))
    assert result["errorCode"] == "invalid_output"
