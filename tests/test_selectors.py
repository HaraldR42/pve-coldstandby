from coldstandby.config import Config
from coldstandby.selectors import (
    DongleSelector,
    HomeAssistantSelector,
    MqttHaSelector,
    build_selectors,
)


def _cfg(**kw) -> Config:
    base = dict(dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def _types(cfg):
    return [type(s) for s in build_selectors(cfg)]


def test_dongle_only_when_nothing_online_configured():
    assert _types(_cfg()) == [DongleSelector]


def test_mqtt_is_the_default_online_selector():
    assert _types(_cfg(mqtt_broker="mqtt.lan")) == [DongleSelector, MqttHaSelector]


def test_rest_ha_used_only_without_mqtt():
    assert _types(_cfg(ha_base_url="http://ha", ha_token="t")) == [
        DongleSelector, HomeAssistantSelector,
    ]


def test_mqtt_wins_when_both_configured():
    cfg = _cfg(mqtt_broker="mqtt.lan", ha_base_url="http://ha", ha_token="t")
    assert _types(cfg) == [DongleSelector, MqttHaSelector]


def test_dongle_is_always_first():
    for cfg in (_cfg(), _cfg(mqtt_broker="m"), _cfg(ha_base_url="h", ha_token="t")):
        assert build_selectors(cfg)[0].__class__ is DongleSelector
