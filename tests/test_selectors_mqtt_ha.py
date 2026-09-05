import json

import pytest

from coldstandby.config import Config
from coldstandby.mode import Mode, ModeDecision, ModeSelector, ModeSelectorUnavailable
from coldstandby.selectors import mqtt_ha
from coldstandby.selectors.mqtt_ha import MqttHaSelector


def _cfg(**kw) -> Config:
    base = dict(
        dongle_marker_token="s",
        mqtt_broker="mqtt.lan",
        node_name="standby01",
        mqtt_timeout_seconds=0.05,
    )
    base.update(kw)
    return Config(**base)


# --- fake paho client --------------------------------------------------

class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


class _Info:
    def __init__(self, published=True):
        self._published = published

    def wait_for_publish(self, timeout=None):
        pass

    def is_published(self):
        return self._published


class FakeClient:
    def __init__(self, *, retained=None, connect_error=None, publish_ok=True):
        self.retained = retained            # str, dict{topic:str}, or None
        self.connect_error = connect_error
        self.publish_ok = publish_ok
        self.on_message = None
        self.published = []                 # (topic, payload, retain)

    def connect(self, host, port, keepalive=60):
        if self.connect_error:
            raise self.connect_error

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        payload = self.retained.get(topic) if isinstance(self.retained, dict) else self.retained
        if payload is not None and self.on_message:
            self.on_message(self, None, _Msg(topic, payload))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))
        return _Info(self.publish_ok)


def _with_client(monkeypatch, client):
    monkeypatch.setattr(MqttHaSelector, "_make_client", lambda self: client)
    return client


def _decision():
    return ModeDecision(
        mode=Mode.LAB,
        decided_by="MqttHaSelector",
        selector_requests={"DongleSelector": "no preference", "MqttHaSelector": "lab"},
    )


# --- mode_requested --------------------------------------------------

def test_is_a_mode_selector():
    assert isinstance(MqttHaSelector(_cfg()), ModeSelector)


def test_topic_base_default():
    assert MqttHaSelector(_cfg())._topic("x") == "pve-coldstandby/standby01/x"


def test_mode_requested_lab(monkeypatch):
    _with_client(monkeypatch, FakeClient(retained="lab"))
    assert MqttHaSelector(_cfg()).mode_requested() is Mode.LAB


@pytest.mark.parametrize("payload", ["replication", "", "  "])
def test_mode_requested_default_or_blank_is_none(monkeypatch, payload):
    _with_client(monkeypatch, FakeClient(retained=payload))
    assert MqttHaSelector(_cfg()).mode_requested() is None


def test_mode_requested_unset_is_none(monkeypatch):
    _with_client(monkeypatch, FakeClient(retained=None))
    assert MqttHaSelector(_cfg()).mode_requested() is None


def test_mode_requested_emergency_is_refused(monkeypatch, caplog):
    _with_client(monkeypatch, FakeClient(retained="emergency"))
    with caplog.at_level("WARNING"):
        assert MqttHaSelector(_cfg()).mode_requested() is None
    assert any("not an option" in r.message for r in caplog.records)


# --- clear ----------------------------------------------------------

def test_clear_resets_next_boot_mode(monkeypatch):
    client = _with_client(monkeypatch, FakeClient())
    MqttHaSelector(_cfg()).clear()
    assert client.published == [
        ("pve-coldstandby/standby01/next_boot_mode", "replication", True)
    ]


def test_clear_raises_when_publish_unconfirmed(monkeypatch):
    _with_client(monkeypatch, FakeClient(publish_ok=False))
    with pytest.raises(ModeSelectorUnavailable):
        MqttHaSelector(_cfg()).clear()


# --- publish_result ------------------------------------------------

def test_publish_result_writes_discovery_and_last_boot(monkeypatch):
    client = _with_client(monkeypatch, FakeClient())
    MqttHaSelector(_cfg()).publish_result(_decision())

    by_topic = {t: (p, r) for t, p, r in client.published}

    disc_topic = "homeassistant/device/pve_coldstandby_standby01/config"
    assert disc_topic in by_topic
    disc = json.loads(by_topic[disc_topic][0])
    assert disc["dev"]["ids"] == ["pve_coldstandby_standby01"]
    assert disc["o"]["name"] == "pve-coldstandby"
    assert disc["cmps"]["next_boot_mode"]["options"] == ["replication", "lab"]
    assert (
        disc["cmps"]["next_boot_mode"]["command_topic"]
        == disc["cmps"]["next_boot_mode"]["state_topic"]
        == "pve-coldstandby/standby01/next_boot_mode"
    )
    # availability deliberately absent -> entities stay usable while node is off
    assert "avty" not in disc and "availability" not in disc
    for comp in disc["cmps"].values():
        assert "availability" not in comp and "avty" not in comp
    assert set(disc["cmps"]) == {
        "next_boot_mode", "last_boot_mode", "last_boot_decided_by",
        "last_boot_host", "last_boot_at", "last_boot_selectors",
    }

    base = "pve-coldstandby/standby01"
    assert by_topic[f"{base}/last_boot_mode"] == ("lab", True)
    assert by_topic[f"{base}/last_boot_decided_by"] == ("MqttHaSelector", True)
    assert json.loads(by_topic[f"{base}/last_boot_selectors"][0])["MqttHaSelector"] == "lab"
    # and it tidies the request back to the default
    assert by_topic[f"{base}/next_boot_mode"] == ("replication", True)


def test_publish_result_without_discovery(monkeypatch):
    client = _with_client(monkeypatch, FakeClient())
    MqttHaSelector(_cfg(mqtt_discovery=False)).publish_result(_decision())
    assert not any("homeassistant/" in t for t, _, _ in client.published)
    assert any(t.endswith("/last_boot_mode") for t, _, _ in client.published)


def test_decided_by_default_when_no_selector_decided(monkeypatch):
    client = _with_client(monkeypatch, FakeClient())
    d = ModeDecision(mode=Mode.REPLICATION, decided_by=None, selector_requests={})
    MqttHaSelector(_cfg()).publish_result(d)
    by_topic = {t: p for t, p, _ in client.published}
    assert by_topic["pve-coldstandby/standby01/last_boot_decided_by"] == "default"


# --- failure modes ------------------------------------------------

def test_connect_failure_is_selector_unavailable(monkeypatch):
    _with_client(monkeypatch, FakeClient(connect_error=ConnectionRefusedError("no")))
    with pytest.raises(ModeSelectorUnavailable):
        MqttHaSelector(_cfg()).mode_requested()


def test_missing_paho_is_selector_unavailable(monkeypatch):
    monkeypatch.setattr(mqtt_ha, "mqtt", None)
    with pytest.raises(ModeSelectorUnavailable):
        MqttHaSelector(_cfg()).mode_requested()
