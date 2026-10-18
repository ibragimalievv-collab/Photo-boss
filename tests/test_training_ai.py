"""Academy transport failure contracts without live API calls or credentials."""
import asyncio
import copy
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services import training_ai as ai
from app.services.training import CATEGORY_BY_SLUG


def jpeg_fixture(size=(60, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, "gray").save(output, "JPEG")
    return output.getvalue()


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
    images = [jpeg_fixture()] * 5
    result = asyncio.run(ai.analyze_training_set(CATEGORY_BY_SLUG["woman"], images, images))
    assert result["errorCode"] == code and result["retryable"] is retryable
    assert "PRIVATE" not in json.dumps(result)


def test_corrupt_academy_image_returns_failure_instead_of_crashing_queue(monkeypatch):
    monkeypatch.setattr(ai, "config", SimpleNamespace(openai_api_key="TEST-ONLY", academy_analysis_model="gpt-4.1-mini"))
    images = [b"not jpeg"] * 5
    result = asyncio.run(ai.analyze_training_set(CATEGORY_BY_SLUG["woman"], images, images))
    assert result["errorCode"] == "invalid_output"


def evidence_review():
    return {"schemaVersion": 2, "score": 90, "decision": "ACCEPT", "critical": False,
            "duplicate_pairs": [], "reshoot_indexes": [], "strengths": ["Лица читаются"], "issues": [],
            "next_action": "Следующая практика", "criteria": {"focus": 18, "light": 14, "composition": 14,
                "pose": 18, "emotion": 13, "variety": 13},
            "frames": [{"index": i, "observation": "На лице мягкий свет", "action": "Сохраните направление света",
                        "uncertainty": "RAW отсутствует", "confidence": "MEDIUM"} for i in range(1, 6)],
            "goal_results": [{"criterion": "Лицо освещено", "status": "ACHIEVED", "frame_indexes": [1, 2, 3, 4, 5],
                              "evidence": "Глаза видны во всех кадрах", "action": "Сохраните направление света"}],
            "correction_checks": [{"previous_issue": "Кадр 2: глубокая тень", "status": "FIXED", "frame_indexes": [2],
                                   "evidence": "Теперь оба глаза видны", "action": "Сохраняйте поворот к окну"}],
            "limitations": ["По JPEG невозможно полностью проверить RAW и пиксельную резкость"]}


def test_live_review_must_cover_each_frame_bound_goal_and_original_error():
    review = evidence_review()
    kwargs = {"require_evidence": True, "assignment_context": {"successCriteria": ["Лицо освещено"]},
              "previous_review": {"sameAssignment": True, "review": {"issues": ["Кадр 2: глубокая тень"]}}}
    assert ai.validate(review, **kwargs) is review
    for change in (lambda r: r["frames"][0].update(index=2),
                   lambda r: r["goal_results"][0].update(criterion="Другое новое задание"),
                   lambda r: r["goal_results"][0].update(status="PARTIAL"),
                   lambda r: r["correction_checks"][0].update(previous_issue="Выдуманная ошибка"),
                   lambda r: r["correction_checks"][0].update(status="STILL_PRESENT")):
        invalid = copy.deepcopy(review)
        change(invalid)
        with pytest.raises(ValueError):
            ai.validate(invalid, **kwargs)
    historical = {k: v for k, v in review.items() if k in ai.SCHEMA["properties"]}
    assert ai.validate(historical) is historical
    with pytest.raises(ValueError):
        ai.validate(historical, require_evidence=True)


def test_earlier_assignment_is_learning_history_without_imposing_old_goals():
    review = evidence_review()
    review["correction_checks"][0]["status"] = "NOT_ASSESSABLE"
    assert ai.validate(review, require_evidence=True,
                       assignment_context={"successCriteria": ["Лицо освещено"]},
                       previous_review={"sameAssignment": False, "review": {"issues": ["Кадр 2: глубокая тень"]}}) is review


def test_technical_metadata_uses_available_exif_and_excludes_location():
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x829A], exif[0x829D], exif[0x8827], exif[0x920A] = 0.005, 2.8, 200, 50
    exif[0x010E] = "private note"
    Image.new("RGB", (60, 40)).save(buffer, "JPEG", exif=exif)
    metadata = ai.technical_metadata(buffer.getvalue())
    assert metadata == {"width": 60, "height": 40, "exposureSeconds": 0.005,
                        "aperture": 2.8, "iso": 200.0, "focalLengthMm": 50.0}
    assert "private" not in json.dumps(metadata)


def test_provider_receives_immutable_goal_and_full_previous_analysis(monkeypatch):
    captured = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(evidence_review())}]}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(ai.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(ai, "config", SimpleNamespace(openai_api_key="TEST-ONLY", academy_analysis_model="gpt-4.1-mini"))
    previous = {"status": "completed", "sameAssignment": True, "review": {
        "issues": ["Кадр 2: глубокая тень"], "next_action": "Повернуться к окну", "score": 72}}
    images = [jpeg_fixture()] * 5
    result = asyncio.run(ai.analyze_training_set(CATEGORY_BY_SLUG["woman"], images, images,
                         assignment_context={"successCriteria": ["Лицо освещено"], "focus": "light"}, previous_review=previous))
    assert result["status"] == "completed"
    prompt = " ".join(item.get("text", "") for item in captured["input"][0]["content"])
    assert "Лицо освещено" in prompt and "Кадр 2: глубокая тень" in prompt and "Повернуться к окну" in prompt
    assert '"score": 72' in prompt


def test_ai_previews_are_bounded_and_report_original_and_analysis_dimensions():
    original = jpeg_fixture((4000, 3000))
    preview, dimensions = ai.analysis_copy(original)
    assert dimensions["original"] == {"width": 4000, "height": 3000, "bytes": len(original)}
    assert dimensions["analysis"]["width"] == 2048 and dimensions["analysis"]["height"] == 1536
    assert dimensions["reencoded"] is True and len(preview) <= ai.MAX_ANALYSIS_BYTES
    with Image.open(io.BytesIO(original)) as source:
        assert source.size == (4000, 3000)
    small = jpeg_fixture()
    assert ai.analysis_copy(small)[0] is small
