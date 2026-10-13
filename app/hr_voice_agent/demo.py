"""Text interview rehearsal and optional SpeechKit voice preview. Never dials anyone.

Usage:
    python -m app.hr_voice_agent.demo
    PB_HR_AI_API_KEY=... PB_HR_AI_FOLDER_ID=... python -m app.hr_voice_agent.demo --live
    PB_HR_SPEECHKIT_API_KEY=... python -m app.hr_voice_agent.demo --intro-ogg demo.ogg
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .brain import HRVoiceAgent, InterviewState
from .tts import YandexSpeechSynthesizer
from .yandex_model import YandexChatModel


class OfflineScriptedModel:
    """Rule-free placeholder for checking dialogue plumbing; NOT an AI model."""
    async def answer(self, messages):
        return (
            "Спасибо за ответ! Я пока работаю в тестовом режиме. "
            "Уточните, пожалуйста, где вы хотели бы работать: в Сочи, Анапе или Крыму?"
        )


async def run(live: bool, intro_ogg: str | None) -> None:
    agent = HRVoiceAgent(YandexChatModel.from_env() if live else OfflineScriptedModel())
    state = InterviewState(candidate_ref="local-preview")
    opening = agent.opening()
    print("MODE:", "YANDEX AI (live API)" if live else "OFFLINE SCRIPTED TEST (not AI)")
    print("ИИ:", opening.speech)

    if intro_ogg:
        speech = YandexSpeechSynthesizer.from_env()
        filename = Path(intro_ogg).expanduser().resolve()
        filename.write_bytes(await speech.synthesize(opening.speech))
        print(f"SpeechKit preview saved: {filename} (no phone call placed)")

    while not state.ended:
        try:
            utterance = input("Кандидат: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nТест завершён.")
            break
        if not utterance:
            continue
        if utterance == "/exit":
            break
        response = await agent.respond(state, utterance)
        print("ИИ:", response.speech)
        if response.status == "handed_off":
            print("[Передача человеку запрошена. Телефония в этой версии не подключена.]")

    if state.consent_for_answers and state.history and live:
        print("\nНейтральное резюме для HR (не отправляется автоматически):")
        try:
            print(await agent.summary_for_hr(state))
        except Exception:
            print("Не удалось подготовить резюме.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Safe local Photo Boss HR agent rehearsal")
    parser.add_argument("--live", action="store_true", help="use Yandex AI Studio API")
    parser.add_argument("--intro-ogg", help="write opening speech audio via SpeechKit (no phone call)")
    args = parser.parse_args()
    asyncio.run(run(args.live, args.intro_ogg))
