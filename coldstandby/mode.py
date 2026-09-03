"""Boot modes, and the pluggable mechanisms that select one.

`Mode` is the closed set of things a boot can do. A `ModeSelector` is one
mechanism for expressing which mode the operator wants next; the concrete
ones live in the `selectors/` package (the dongle, an online switch, and
whatever you add). `determine_mode` walks a priority-ordered list of them
and takes the first that has an opinion; if none do, the answer is
Replication.

Selectors are consulted in order, so priority is just list position -- the
list is assembled in `selectors.build_selectors`. The dongle is always
first (it's the only one that works with the network down) and mandatory.

Failure of any one selector (`ModeSelectorUnavailable`) is not fatal: it is
skipped and the next is tried, ultimately falling to Replication. That mode
only overwrites this node's own scratch copies and then powers off -- it
never reaches `main`, the cluster, the read-only backup archives, or
anything running elsewhere, and it starts no guests. That bounded blast
radius is what makes it the safe default.
"""
from __future__ import annotations

import abc
import enum
import logging
from typing import Sequence

log = logging.getLogger(__name__)


class Mode(enum.Enum):
    EMERGENCY = "emergency"
    LAB = "lab"
    REPLICATION = "replication"


class ModeSelectorUnavailable(Exception):
    """A selector could not be consulted (unreachable, unusable answer, …).

    Callers MUST treat this as "this selector has no say right now", never
    as something to retry-and-guess around.
    """


class ModeSelector(abc.ABC):
    """One mechanism for choosing the next boot mode."""

    @abc.abstractmethod
    def mode_requested(self) -> Mode | None:
        """The mode this mechanism currently requests, or ``None`` if it
        has no opinion. Raises `ModeSelectorUnavailable` if it cannot be
        consulted at all."""

    def clear(self) -> None:
        """Consume a one-shot request after it has been acted on, so it
        can't re-trigger on the next unattended boot. Stateless selectors
        (the dongle -- you physically remove it) leave this a no-op.

        May raise `ModeSelectorUnavailable`; by then the mode for this boot
        is already decided, so the caller only logs it."""


def determine_mode(selectors: Sequence[ModeSelector]) -> Mode:
    """First selector (in priority order) with an opinion wins."""
    for selector in selectors:
        name = type(selector).__name__
        try:
            requested = selector.mode_requested()
        except ModeSelectorUnavailable as exc:
            log.warning("%s unavailable (%s) -- skipping.", name, exc)
            continue

        if requested is None:
            log.debug("%s has no preference.", name)
            continue

        log.info("%s selects %s mode.", name, requested.value)
        try:
            selector.clear()
        except ModeSelectorUnavailable as exc:
            log.error(
                "%s: could not clear after consuming its request (%s) -- "
                "reset it by hand.",
                name, exc,
            )
        return requested

    log.info("No selector expressed a preference -> Replication.")
    return Mode.REPLICATION
