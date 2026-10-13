"""Regression checks for receipts that Telegram refuses to treat as photos."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto

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
        message="Bad Request: chat not found",
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
