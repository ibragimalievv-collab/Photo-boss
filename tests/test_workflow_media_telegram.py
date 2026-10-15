"""Regression checks for receipts that Telegram refuses to treat as photos."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendDocument, SendPhoto

from app.miniapp_security import AccessError
from app.workflow import Workflow


def workflow_with_bot(photo_error=None):
    attachment = SimpleNamespace(file_id="document-id", file_unique_id="document-unique-id")
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=photo_error),
        send_document=AsyncMock(return_value=SimpleNamespace(document=attachment)),
    )
    return Workflow(SimpleNamespace(bot=bot)), bot


def test_receipt_bad_dimensions_retries_as_document_without_modifying_original():
    error = TelegramBadRequest(
        method=SendPhoto(chat_id=42, photo="fixture"),
        message="Bad Request: PHOTO_INVALID_DIMENSIONS",
    )
    workflow, bot = workflow_with_bot(error)
    raw = b"\xff\xd8\xff" + b"original-receipt" * 15
    result = asyncio.run(workflow.save_telegram(SimpleNamespace(tg_id=42), raw, receipt=True))
    assert result == ("document-id", "document-unique-id")
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    args, kwargs = bot.send_document.await_args
    assert args[0] == 42
    assert args[1].data == raw
    assert kwargs["protect_content"] is True


def test_unrelated_telegram_errors_are_not_hidden_by_receipt_fallback():
    error = TelegramBadRequest(
        method=SendPhoto(chat_id=42, photo="fixture"),
        message="Bad Request: DOCUMENT_INVALID",
    )
    workflow, bot = workflow_with_bot(error)
    with pytest.raises(TelegramBadRequest):
        asyncio.run(workflow.save_telegram(SimpleNamespace(tg_id=42), b"\xff\xd8\xff" + b"x" * 150, receipt=True))
    bot.send_document.assert_not_awaited()


def test_shooting_photo_does_not_send_or_require_receipt():
    workflow, bot = workflow_with_bot()
    raw = b"\xff\xd8\xff" + b"shooting-photo" * 15
    result = asyncio.run(workflow.save_telegram(SimpleNamespace(tg_id=42), raw))
    assert result == ("document-id", "document-unique-id")
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_awaited_once()


@pytest.mark.parametrize('error_type,message', [
    (TelegramForbiddenError, 'Forbidden: bot was blocked by the user'),
    (TelegramBadRequest, 'Bad Request: chat not found'),
])
def test_unavailable_bot_chat_requires_manual_retry_of_same_original(error_type, message):
    workflow, bot = workflow_with_bot()
    archived = bot.send_document.return_value
    bot.send_document.side_effect = [error_type(method=SendDocument(chat_id=42, document='fixture'), message=message), archived]
    raw = b'\xff\xd8\xff' + b'original-before-chat-error' * 15
    actor = SimpleNamespace(tg_id=42)
    with pytest.raises(AccessError) as error:
        asyncio.run(workflow.save_telegram(actor, raw))
    assert error.value.status == 409
    assert 'Старт' in str(error.value) and 'очереди' in str(error.value)
    assert bot.send_document.await_count == 1
    assert asyncio.run(workflow.save_telegram(actor, raw)) == ('document-id', 'document-unique-id')
    assert [call.args[1].data for call in bot.send_document.await_args_list] == [raw, raw]


@pytest.mark.parametrize('error_kind', ['network', 'server', 'rate_limit', 'timeout'])
def test_transient_telegram_failure_can_retry_the_unchanged_original(error_kind):
    workflow, bot = workflow_with_bot()
    archived = bot.send_document.return_value
    method = SendDocument(chat_id=42, document='fixture')
    failures = {
        'network': TelegramNetworkError(method=method, message='connection reset'),
        'server': TelegramServerError(method=method, message='upstream unavailable'),
        'rate_limit': TelegramRetryAfter(method=method, message='too many requests', retry_after=3),
        'timeout': TimeoutError(),
    }
    bot.send_document.side_effect = [failures[error_kind], archived]
    raw = b'\xff\xd8\xff' + b'original-before-network-error' * 15
    actor = SimpleNamespace(tg_id=42)
    with pytest.raises(AccessError) as error:
        asyncio.run(workflow.save_telegram(actor, raw))
    assert error.value.status == 503
    assert 'очереди' in str(error.value)
    assert bot.send_document.await_count == 1
    assert asyncio.run(workflow.save_telegram(actor, raw)) == ('document-id', 'document-unique-id')
    assert [call.args[1].data for call in bot.send_document.await_args_list] == [raw, raw]
