"""Offline checks for shooting photo storage sync."""
import pytest
import app.services.photo_storage as storage
from app.yandex_disk import ROOT, safe_path

@pytest.mark.parametrize("payload,expected", [
    (b"\xff\xd8\xff" + b"x" * 100, ("jpg", "image/jpeg")),
    (b"\x89PNG\r\n\x1a\n" + b"x" * 100, ("png", "image/png")),
    (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 100, ("webp", "image/webp")),
])
def test_image_format(payload, expected):
    assert storage.image_format(payload) == expected

def test_non_image_rejected():
    with pytest.raises(ValueError):
        storage.image_format(b"not-an-image" * 20)

def test_limited_buffer_enforces_cap(monkeypatch):
    monkeypatch.setattr(storage, "MAX_TELEGRAM_IMAGE_BYTES", 10)
    buffer = storage.LimitedBuffer()
    buffer.write(b"12345")
    with pytest.raises(ValueError):
        buffer.write(b"678901")

def test_generated_storage_path_is_inside_app_folder():
    path = ROOT + "/shootings/123/456_abcdef012345.jpg"
    assert safe_path(path) == path

def test_storage_limits_are_bounded():
    assert storage.MAX_ATTEMPTS == 5
    assert storage.STALE_UPLOAD.total_seconds() == 600
