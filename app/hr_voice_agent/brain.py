"""Independent text-dialogue brain for the future Photo Boss HR voice agent.

No phone calls, audio, database writes or production routes are enabled here.
An external consented call controller may feed final STT utterances into this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


class DialogueModel(Protocol):
    async def answer(self, messages: list[dict[str, str]]) -> str:
        """Return one brief Russian-language assistant response."""


SYSTEM_PROMPT = """Ты — голосовой ИИ-рекрутер Photo Boss. Представляйся как ИИ,
никогда не притворяйся живым человеком. Веди доброжелательный, естественный
разговор по-русски, слушай собеседника, отвечай на его вопрос прежде,
чем задавать следующий. Один вопрос за раз; обычно до 2–3 предложений.

Правдивые условия работы Photo Boss:
- Вакансии: фотограф и менеджер по записи; регионы: Сочи, Анапа, Крым.
- Опыт не обязателен: обучение предоставляется.
- Работа с туристами в гостиницах, оплата процентом от результатов.
- Выплаты еженедельные. Нельзя гарантировать доход или называть
  выдуманный оклад, зарплату, стоимость проживания либо точные даты старта.
- Жильё: только частичная помощь, НЕ бесплатное жильё.
- Проезд: компенсируется после прибытия и минимум одного месяца работы;
  не обещай оплату билетов заранее.
- Менеджер рассказывает гостям об услуге, отвечает на вопросы и ведёт запись.
- Фотограф проводит съёмки, общается с гостями и готовит фотографии к продаже.

Для первичного разговора тактично уточни:
1. Фотографом или менеджером хочет работать человек?
2. Из какого он города, где готов работать и возможен ли переезд?
3. Есть ли опыт работы с людьми и фотографией?
4. Когда готов приступить?
5. Какие вопросы остались у кандидата?

Не запрашивай паспорт, банковские данные, адрес проживания, дату рождения,
сведения о здоровье, религии и другие лишние чувствительные данные.
Не оценивай пригодность по защищённым признакам и не принимай
автоматическое решение о приёме или отказе.
Если кандидат спрашивает неизвестное — честно скажи, что уточнит сотрудник.
По просьбе переключи на человека; не пытайся удержать человека в разговоре.
Кратко веди диалог; не придумывай информацию о кандидате.
"""


CONSENT_QUESTION = (
    "Здравствуйте! Я голосовой ИИ-помощник Photo Boss. "
    "Вы оставляли заявку на работу. Вам удобно сейчас поговорить? "
    "Согласны, чтобы ваши ответы использовались для рассмотрения анкеты?"
)
OPT_OUT = (
    "Понял. Разговор завершаю. Передам сотруднику, что вы не хотите "
    "продолжать. Всего доброго!"
)
HANDOFF = (
    "Хорошо. Передам ваш запрос сотруднику Photo Boss, "
    "он свяжется с вами согласованным способом."
)
# Opt-out wins even if the speaker also uses a word that looks like consent.
DECLINE = re.compile(
    r"(не\s+соглас|не\s+надо|не\s+хочу|не\s+интерес|"
    r"не\s+звоните|больше\s+не\s+звон|отказыва|"
    r"удалите\s+мой|прекрат|до\s+свидания)", re.IGNORECASE
)
AGREE = re.compile(
    r"^(да\b|соглас(ен|на|ны)\b|можно\b|"
    r"удобно\b|хорошо\b|конечно\b|ок\b)", re.IGNORECASE
)
TRANSFER = re.compile(
    r"(соедин(ите|и)\s+с|жив(ым|ого)\s+человек|"
    r"оператор(ом|у|а)?\b|сотрудник(ом|у|а)?\b|"
    r"позовите\s+человек)", re.IGNORECASE
)


def normalized_russian_mobile(phone: str) -> str | None:
    """Only a well-formed Russian +7 mobile number; no outbound call is made."""
    compact = re.sub(r"[\s()\-\u2013]", "", phone)
    if compact.startswith("8") and len(compact) == 11:
        compact = "+7" + compact[1:]
    if re.fullmatch(r"\+79\d{9}", compact):
        return compact
    return None


def may_call_candidate(phone: str, *, explicit_call_consent: bool) -> bool:
    """The Telegram HR questionnaire consent alone does not authorize calls."""
    return explicit_call_consent and normalized_russian_mobile(phone) is not None


@dataclass
class InterviewState:
    candidate_ref: str
    consent_for_answers: bool = False
    ended: bool = False
    transferred_to_human: bool = False
    history: list[dict[str, str]] = field(default_factory=list)
    user_turns: int = 0


@dataclass(frozen=True)
class AgentTurn:
    speech: str
    status: str  # needs_consent / talking / handed_off / ended


class HRVoiceAgent:
    def __init__(self, model: DialogueModel):
        self.model = model

    @staticmethod
    def opening() -> AgentTurn:
        return AgentTurn(CONSENT_QUESTION, "needs_consent")

    async def respond(self, state: InterviewState, recognized_text: str) -> AgentTurn:
        """Call with FINAL speech recognition only, not noisy partial chunks."""
        if state.ended:
            return AgentTurn("Разговор уже завершён.", "ended")

        text = recognized_text.strip()[:1000]
        if not text:
            return AgentTurn("Повторите, пожалуйста, я не расслышал.", "talking" if state.consent_for_answers else "needs_consent")

        if DECLINE.search(text):
            state.ended = True
            state.consent_for_answers = False
            state.history.clear()
            state.user_turns = 0
            return AgentTurn(OPT_OUT, "ended")

        if not state.consent_for_answers:
            if AGREE.search(text):
                state.consent_for_answers = True
                return AgentTurn(
                    "Спасибо! Какая работа вам интереснее — фотограф или менеджер по записи?",
                    "talking",
                )
            return AgentTurn(CONSENT_QUESTION, "needs_consent")

        if TRANSFER.search(text):
            state.transferred_to_human = True
            state.ended = True
            return AgentTurn(HANDOFF, "handed_off")

        if state.user_turns >= 30:
            state.transferred_to_human = True
            state.ended = True
            return AgentTurn(HANDOFF, "handed_off")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *state.history[-20:],
            {"role": "user", "content": text},
        ]
        try:
            answer = (await self.model.answer(messages)).strip()
        except Exception:
            # Secrets, stack traces and candidate details must not go to a caller.
            state.ended = True
            state.transferred_to_human = True
            return AgentTurn(
                "Извините, связь с ИИ временно недоступна. "
                "Я передам запрос сотруднику, чтобы с вами связались.",
                "handed_off",
            )
        if not answer:
            state.ended = True
            state.transferred_to_human = True
            return AgentTurn(HANDOFF, "handed_off")
        answer = answer[:650]
        state.history.extend((
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ))
        state.user_turns += 1
        return AgentTurn(answer, "talking")

    async def summary_for_hr(self, state: InterviewState) -> str:
        """Facts only; a human makes all recruiting decisions."""
        if not state.consent_for_answers:
            return "Нет согласия на обработку ответов: резюме не формируется."
        if not state.history:
            return "Кандидат согласился поговорить, но содержательных ответов пока нет."
        messages = [
            {"role": "system", "content":
             "Кратко изложи только сообщённые кандидатом факты: должность, "
             "регион и переезд, опыт, срок выхода, вопросы и пожелания. "
             "Не придумывай сведения, не ставь оценку, не рекомендуй отказ/приём. "
             "Ответ на русском языке без персональных данных."},
            *state.history[-30:],
            {"role": "user", "content": "Составь нейтральное HR-резюме беседы."},
        ]
        return (await self.model.answer(messages)).strip()[:2000]
