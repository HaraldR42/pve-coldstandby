import io
import json
import urllib.error

import pytest

from coldstandby.config import Config
from coldstandby.selectors.home_assistant import HomeAssistantSelector
from coldstandby.mode import Mode, ModeSelector, ModeSelectorUnavailable


def _cfg(**kw) -> Config:
    base = dict(
        ha_base_url="http://ha:8123", ha_token="tok", dongle_marker_token="s"
    )
    base.update(kw)
    return Config(**base)


def _resp(payload):
    body = json.dumps(payload).encode()

    class _Ctx:
        def __enter__(self):
            return io.BytesIO(body)

        def __exit__(self, *a):
            return False

    return _Ctx()


def test_is_a_mode_selector():
    assert isinstance(HomeAssistantSelector(_cfg()), ModeSelector)


def test_mode_requested_lab_or_nothing(monkeypatch):
    sel = HomeAssistantSelector(_cfg())

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _resp({"state": "lab"}))
    assert sel.mode_requested() is Mode.LAB

    # anything that isn't the lab option -> no opinion (never "requests
    # Replication"; that's just the fallback)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _resp({"state": "replication"}))
    assert sel.mode_requested() is None


def test_clear_posts_replication_option(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["body"] = json.loads(req.data)
        return _resp({})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    HomeAssistantSelector(_cfg()).clear()

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/services/input_select/select_option")
    assert seen["body"] == {
        "entity_id": "input_select.coldstandby_mode",
        "option": "replication",
    }


def test_network_error_becomes_selector_unavailable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ModeSelectorUnavailable):
        HomeAssistantSelector(_cfg()).mode_requested()


def test_garbled_response_becomes_selector_unavailable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _resp({"nope": 1}))
    with pytest.raises(ModeSelectorUnavailable):
        HomeAssistantSelector(_cfg()).mode_requested()
