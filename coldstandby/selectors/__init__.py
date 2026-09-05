"""Mode selectors: the pluggable mechanisms that choose the boot mode.

Each module here defines one `ModeSelector` (the interface lives in
`coldstandby.mode`). `build_selectors` assembles them into the
priority-ordered list `determine_mode` walks.

To add a new mechanism -- a plain REST endpoint, a flag file on an
always-on host, a BMC next-boot flag -- do two things:

1. Add a module here with a `ModeSelector` subclass. Its `mode_requested()`
   returns a `Mode` or `None` (no opinion), and raises
   `ModeSelectorUnavailable` if it can't be consulted at all.
2. Wire it into `build_selectors` at the priority you want (later = lower
   priority). The dongle stays first: it is the only selector that works
   with the network down.

Shipped selectors:

* `DongleSelector`        -- USB stick, local only, always first, mandatory.
* `MqttHaSelector`        -- online lab switch over MQTT + HA discovery;
                             the online selector used by default.
* `HomeAssistantSelector` -- the original REST `input_select` selector;
                             kept, but only used when MQTT is not configured.
"""
from __future__ import annotations

from ..config import Config
from ..mode import ModeSelector
from .dongle import DongleSelector
from .home_assistant import HomeAssistantSelector
from .mqtt_ha import MqttHaSelector

__all__ = [
    "DongleSelector",
    "MqttHaSelector",
    "HomeAssistantSelector",
    "build_selectors",
]


def build_selectors(cfg: Config) -> list[ModeSelector]:
    """The selectors to consult, in priority order.

    The dongle is always first and always present. Then one online lab
    selector, if configured: MQTT when `mqtt_broker` is set, otherwise the
    legacy REST Home Assistant selector.
    """
    selectors: list[ModeSelector] = [DongleSelector(cfg)]
    if cfg.mqtt_enabled:
        selectors.append(MqttHaSelector(cfg))
    elif cfg.online_selector_enabled:
        selectors.append(HomeAssistantSelector(cfg))
    return selectors
