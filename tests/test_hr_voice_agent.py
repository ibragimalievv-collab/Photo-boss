"""Offline regression checks for an isolated future HR voice call controller."""
import asyncio

import pytest

from app.hr_voice_agent.brain import (
    HRVoiceAgent,
    InterviewState,
    may_call_candidate,
    normalized_russian_mobile,
    SYSTEM_PROMPT,
)
from app.hr_voice_agent.yandex_model import (
    ModelConfigurationError,
    YandexChatModel,
    build_payload,
    parse_completion,
)


class FakeModel:
    def __init__(self, reply="Спасибо! В каком городе хотите работать?"):
        self.reply = reply
        self.requests = []

    async def answer(self, messages):
        self.requests.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_outbound_requires_separate_explicit_call_consent():
    assert normalized_russian_mobile("8 (999) 123-45-67") == "+79991234567"
    assert normalized_russian_mobile("+7 999 123-45-67") == "+79991234567"
    assert normalized_russian_mobile("+798") is None
    assert normalized_russian_mobile("+78005553535") is None
    assert not may_call_candidate("+79991234567", explicit_call_consent=False)
    assert may_call_candidate("+79991234567", explicit_call_consent=True)
    assert not may_call_candidate("+78005553535", explicit_call_consent=True)


def test_clear_consent_required_before_llm_gets_candidate_answers():
    async def scenario():
        model = FakeModel()
        agent = HRVoiceAgent(model)
        state = InterviewState(candidate_ref="example")
        assert agent.opening().status == "needs_consent"
        rejected = await agent.respond(state, "Я фотограф, сколько платите?")
        assert rejected.status == "needs_consent"
        assert model.requests == []
        consent = await agent.respond(state, "Да, согласен")
        assert consent.status == "talking"
        answer = await agent.respond(state, "Хочу фотографом, есть жильё?")
        assert answer.status == "talking"
        assert model.requests[0][0]["role"] == "system"
        assert "НЕ бесплатное жильё" in SYSTEM_PROMPT
        assert model.requests[0][-1]["content"] == "Хочу фотографом, есть жильё?"
        assert state.history[-2]["role"] == "user"
        summary = await agent.summary_for_hr(state)
        assert summary and len(model.requests) == 2

    asyncio.run(scenario())


def test_refusal_ends_and_never_sends_text_to_model():
    async def scenario():
        model = FakeModel()
        agent = HRVoiceAgent(model)
        state = InterviewState(candidate_ref="example")
        result = await agent.respond(state, "Да, но я не согласен")
        assert result.status == "ended"
        assert state.ended
        assert not state.consent_for_answers
        assert model.requests == []
        assert (await agent.respond(state, "Сколько платят?")).status == "ended"

    asyncio.run(scenario())


def test_human_handoff_and_fallback():
    async def scenario():
        agent = HRVoiceAgent(FakeModel())
        state = InterviewState(candidate_ref="example", consent_for_answers=True)
        result = await agent.respond(state, "Соедините с оператором, пожалуйста")
        assert result.status == "handed_off"
        assert state.transferred_to_human
        assert state.ended

        bad = HRVoiceAgent(FakeModel(RuntimeError("secret should never leak")))
        state = InterviewState(candidate_ref="example", consent_for_answers=True)
        result = await bad.respond(state, "Расскажите о графике")
        assert result.status == "handed_off"
        assert "secret" not in result.speech

    asyncio.run(scenario())


def test_yandex_adapter_payload_validation_and_parsing():
    payload = build_payload("b1g123folder", "yandexgpt/latest", [
        {"role": "system", "content": "Ты HR-бот"},
        {"role": "user", "content": "Добрый день"},
    ])
    assert payload["model"] == "gpt://b1g123folder/yandexgpt/latest"
    assert payload["temperature"] <= 0.5
    assert payload["stream"] is False
    assert parse_completion({"choices": [{"message": {"content": "Привет!"}}]}) == "Привет!"
    with pytest.raises(ValueError):
        parse_completion({"choices": []})
    with pytest.raises(ModelConfigurationError):
        YandexChatModel(api_key="", folder_id="b1g123folder")
    with pytest.raises(ModelConfigurationError):
        build_payload("../../bad", "yandexgpt/latest", [{"role": "user", "content": "x"}])
    with pytest.raises(ValueError):
        build_payload("b1g123folder", "yandexgpt/latest",
                      [{"role": "developer", "content": "x"}])
