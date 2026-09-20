"""Work-chat input and disclosure checks."""
import pytest

from app.miniapp_security import AccessError
from app.work_chat import MAX_MESSAGE, RETENTION_DAYS, clean_message, positive_id
from app.work_rules import WORK_RULES_TEXT, WORK_RULES_VERSION, work_rules_hash


@pytest.mark.parametrize("bad", [True, 0, -1, "0", "x", "", 2**31])
def test_bad_chat_ids(bad):
    with pytest.raises(AccessError):
        positive_id(bad)


def test_message_cleaning_and_limits():
    assert clean_message("  привет\r\nкоманда  ") == "привет\nкоманда"
    assert clean_message("x" * MAX_MESSAGE) == "x" * MAX_MESSAGE
    for bad in ("", "   ", "x" * (MAX_MESSAGE + 1), "ok\x00bad"):
        with pytest.raises(AccessError):
            clean_message(bad)


def test_rules_are_explicit_and_whole_document_acceptance():
    assert "Владелец компании имеет доступ" in WORK_RULES_TEXT
    assert "включая сообщения между двумя сотрудниками" in WORK_RULES_TEXT
    assert "не является договором оказания услуг" in WORK_RULES_TEXT
    assert str(RETENTION_DAYS) in WORK_RULES_TEXT
    assert len(work_rules_hash()) == 64
    assert WORK_RULES_VERSION
