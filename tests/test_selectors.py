from coldstandby.config import Config
from coldstandby.selectors import (
    DongleSelector,
    HomeAssistantSelector,
    build_selectors,
)


def _cfg(**kw) -> Config:
    base = dict(dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def test_dongle_only_when_no_online_selector():
    selectors = build_selectors(_cfg())
    assert [type(s) for s in selectors] == [DongleSelector]


def test_online_selector_appended_after_dongle_when_configured():
    selectors = build_selectors(_cfg(ha_base_url="http://ha", ha_token="t"))
    assert [type(s) for s in selectors] == [DongleSelector, HomeAssistantSelector]
