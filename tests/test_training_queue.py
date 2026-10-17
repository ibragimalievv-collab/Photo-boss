"""The Telegram entry point delegates to the shared web review queue."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.handlers import training


def test_bot_does_not_override_review_claimed_by_web_worker(monkeypatch):
    queue = SimpleNamespace(process_pending=AsyncMock(return_value={}))
    monkeypatch.setattr(training, "AcademyPractice", Mock(return_value=queue))
    sessions = Mock()
    monkeypatch.setattr(training, "Session", sessions)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert asyncio.run(training.ai_review_assignment(bot, 42)) is True
    assert queue.process_pending.await_args.kwargs == {"assignment_id": 42}
    sessions.assert_not_called()
    bot.send_message.assert_not_awaited()


def test_bot_explains_shared_queue_retry_without_overwriting_result(monkeypatch):
    failure = {"status": "unavailable", "errorCode": "timeout", "retryable": True,
               "nextRetryAt": "2099-01-01T00:00:00+00:00", "message": "Ваш текст сохранён."}
    queue = SimpleNamespace(process_pending=AsyncMock(return_value={42: failure}))
    monkeypatch.setattr(training, "AcademyPractice", Mock(return_value=queue))
    get = AsyncMock(side_effect=[SimpleNamespace(user_id=3, category_slug="woman"), SimpleNamespace(tg_id=1003)])

    class Session:
        async def __aenter__(self):
            return SimpleNamespace(get=get)

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(training, "Session", Session)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert asyncio.run(training.ai_review_assignment(bot, 42)) is True
    message = bot.send_message.await_args.args[1]
    assert "Фотографии сохранены" in message
    assert "запланирована автоматически" in message
