from app.core.config import DEFAULT_CONFIG


def test_internet_is_disabled_by_default():
    assert DEFAULT_CONFIG.internet_enabled is False


def test_push_to_talk_is_enabled():
    assert DEFAULT_CONFIG.push_to_talk is True


def test_confirmation_is_required():
    assert DEFAULT_CONFIG.require_confirmation is True

def test_app_identity():
    assert DEFAULT_CONFIG.app_name == "Anaya"
    assert DEFAULT_CONFIG.version == "0.1.0"