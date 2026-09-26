from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_SOURCE = (ROOT / "app" / "webapp" / "js" / "api.js").read_text(encoding="utf-8")
SECURITY_SOURCE = (ROOT / "app" / "miniapp_security.py").read_text(encoding="utf-8")


def test_miniapp_keeps_signed_telegram_auth_in_secure_storage():
    assert "SecureStorage" in API_SOURCE
    assert "photo_boss_init_data_v1" in API_SOURCE
    assert "secureSet(SECURE_INIT_KEY,value)" in API_SOURCE
    assert "secureGet(SECURE_INIT_KEY)" in API_SOURCE
    assert "localStorage.setItem" not in API_SOURCE


def test_cached_init_data_is_age_bounded_and_server_still_validates_it():
    assert "SESSION_MAX_AGE_SECONDS=23*60*60" in API_SOURCE
    assert "MINIAPP_SESSION_MAX_AGE = 24 * 60 * 60" in SECURITY_SOURCE
    assert "validate_init_data" in SECURITY_SOURCE


def test_owner_fallback_survives_hash_navigation_and_can_recover_from_401():
    assert "SESSION_OWNER_KEY='pb_owner_launch_v1'" in API_SOURCE
    assert "sessionSet(SESSION_OWNER_KEY,value)" in API_SOURCE
    assert "response.status===401&&alternate" in API_SOURCE
    assert "X-PhotoBoss-Owner-Launch" in API_SOURCE


def test_api_no_longer_discards_same_origin_session_state():
    assert "credentials:'same-origin'" in API_SOURCE
    assert "credentials:'omit'" not in API_SOURCE


def test_failed_cached_credential_is_removed_instead_of_looping_forever():
    assert "forgetInitData()" in API_SOURCE
    assert "secureRemove(SECURE_INIT_KEY)" in API_SOURCE
