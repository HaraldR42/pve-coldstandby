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
        self.on_connect = None
        self.on_disconnect = None
        self.published = []                 # (topic, payload, retain)
        self.will = None                    # (topic, payload, retain)
        self.loop_running = False
        self.disconnected = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect(self, host, port, keepalive=60):
        if self.connect_error:
            raise self.connect_error

    def connect_async(self, host, port, keepalive=60):
        if self.connect_error:
            raise self.connect_error

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.disconnected = True

    def fire_connected(self, reason_code=0):
        self.on_connect(self, None, {}, reason_code, None)

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


def test_info_messages_narrate_the_lab_flow(monkeypatch, caplog):
    _with_client(monkeypatch, FakeClient(retained="lab"))
    sel = MqttHaSelector(_cfg())
    with caplog.at_level("INFO", logger="coldstandby.selectors.mqtt_ha"):
        sel.mode_requested()
        sel.clear()
        sel.publish_result(_decision())
    msgs = " | ".join(r.getMessage() for r in caplog.records if r.levelname == "INFO")
    assert "Checking MQTT" in msgs
    assert "requesting Lab mode" in msgs
    assert "request consumed" in msgs
    assert "Publishing boot result" in msgs
    assert "Home Assistant discovery" in msgs
    assert "boot result published" in msgs


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
        "next_boot_mode", "online", "last_boot_mode", "last_boot_decided_by",
        "last_boot_host", "last_boot_at", "last_boot_selectors",
    }
    online = disc["cmps"]["online"]
    assert online["p"] == "binary_sensor"
    assert online["device_class"] == "connectivity"
    assert online["state_topic"] == "pve-coldstandby/standby01/online"
    assert (online["payload_on"], online["payload_off"]) == ("true", "false")

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


# --- MqttPresence ------------------------------------------------------

from coldstandby.selectors.mqtt_ha import MqttPresence  # noqa: E402


def _presence(monkeypatch, client):
    monkeypatch.setattr(mqtt_ha, "_build_client", lambda cfg: client)
    return MqttPresence(_cfg())


def test_presence_sets_lwt_and_publishes_online(monkeypatch):
    client = FakeClient()
    p = _presence(monkeypatch, client)
    p.start()

    assert client.will == ("pve-coldstandby/standby01/online", "false", True)
    assert client.loop_running is True
    assert p.active is True

    client.fire_connected()  # broker accepts the connection
    assert ("pve-coldstandby/standby01/online", "true", True) in client.published


def test_presence_connect_refused_does_not_publish_online(monkeypatch):
    client = FakeClient()
    p = _presence(monkeypatch, client)
    p.start()
    client.fire_connected(reason_code=1)  # non-zero -> failure
    assert not any(t.endswith("/online") and pl == "true" for t, pl, _ in client.published)


def test_presence_stop_marks_offline_and_disconnects(monkeypatch):
    client = FakeClient()
    p = _presence(monkeypatch, client)
    p.start()
    p.stop()

    assert ("pve-coldstandby/standby01/online", "false", True) in client.published
    assert client.disconnected is True
    assert client.loop_running is False
    assert p.active is False
    p.stop()  # idempotent


def test_presence_without_paho_is_inert(monkeypatch):
    monkeypatch.setattr(mqtt_ha, "mqtt", None)
    p = MqttPresence(_cfg())
    p.start()
    assert p.active is False
    p.stop()  # must not raise


def test_presence_connect_error_leaves_it_inactive(monkeypatch):
    client = FakeClient(connect_error=ValueError("bad host"))
    monkeypatch.setattr(mqtt_ha, "_build_client", lambda cfg: client)
    p = MqttPresence(_cfg())
    p.start()
    assert p.active is False
