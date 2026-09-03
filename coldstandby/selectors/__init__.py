"""Mode selectors: the pluggable mechanisms that choose the boot mode.

Each module here defines one `ModeSelector` (the interface lives in
`coldstandby.mode`). `build_selectors` assembles them into the
priority-ordered list `determine_mode` walks.

To add a new mechanism -- an MQTT topic, a plain REST endpoint, a flag
file on an always-on host, a BMC next-boot flag -- do two things:

1. Add a module here with a `ModeSelector` subclass. Its `mode_requested()`
   returns a `Mode` or `None` (no opinion), and raises
   `ModeSelectorUnavailable` if it can't be consulted at all.
2. Append it to the list in `build_selectors` at the priority you want
   (later = lower priority). The dongle stays first: it is the only
   selector that works with the network down.
"""
from __future__ import annotations

from ..config import Config
from ..mode import ModeSelector
from .dongle import DongleSelector
from .home_assistant import HomeAssistantSelector

__all__ = ["DongleSelector", "HomeAssistantSelector", "build_selectors"]


def build_selectors(cfg: Config) -> list[ModeSelector]:
    """The selectors to consult, in priority order.

    The dongle is always first and always present. The online (Home
    Assistant) selector follows it when configured.
    """
    selectors: list[ModeSelector] = [DongleSelector(cfg)]
    if cfg.online_selector_enabled:
        selectors.append(HomeAssistantSelector(cfg))
    return selectors
