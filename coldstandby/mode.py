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

Once the mode is settled, `determine_mode` hands a `ModeDecision` to
*every* selector's `publish_result` -- not just the one that decided -- so
any of them can surface the outcome (a Home Assistant sensor, an MQTT
topic, a status LED). Publishing is best-effort and never changes the
decision.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import enum
import logging
import socket
from typing import Sequence

log = logging.getLogger(__name__)


class Mode(enum.Enum):
    EMERGENCY = "emergency"
    LAB = "lab"
    REPLICATION = "replication"


# What a selector's mode_requested() amounted to, for the status report.
REQUEST_UNAVAILABLE = "unavailable"
REQUEST_NONE = "no preference"
REQUEST_NOT_CONSULTED = "not consulted"  # a lower-priority selector, after the decision


class ModeSelectorUnavailable(Exception):
    """A selector could not be consulted (unreachable, unusable answer, …).

    Callers MUST treat this as "this selector has no say right now", never
    as something to retry-and-guess around.
    """


@dataclasses.dataclass(frozen=True)
class ModeDecision:
    """The outcome of resolution, handed to every selector afterwards."""

    mode: Mode
    # Class name of the selector that decided, or None if nothing had a
    # preference and we fell through to the default.
    decided_by: str | None
    # Every active selector -> what it contributed: a mode value, or one of
    # the REQUEST_* constants above.
    selector_requests: dict[str, str]
    resolved_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    host: str = dataclasses.field(default_factory=socket.gethostname)

    def summary(self) -> str:
        by = self.decided_by or "no selector; default"
        return f"{self.mode.value} ({by})"

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "decided_by": self.decided_by,
            "host": self.host,
            "resolved_at": self.resolved_at.isoformat(timespec="seconds"),
            "selectors": dict(self.selector_requests),
        }


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

    def publish_result(self, decision: "ModeDecision") -> None:
        """Surface the resolved decision somewhere visible. Called once on
        every active selector -- including ones that had no opinion or were
        unavailable -- after the mode is settled.

        Best-effort: the mode is already decided, so any exception here is
        logged and swallowed by the caller. Default is a no-op (a USB stick
        has nothing to report to)."""


def determine_mode(selectors: Sequence[ModeSelector], *, dry_run: bool = False) -> Mode:
    """First selector (in priority order) with an opinion wins; then the
    decision is published to all of them.

    ``dry_run`` resolves the mode but changes nothing outward: the winning
    selector's request is *not* consumed (`clear`) and the decision is
    *not* published (`publish_result`), so a preview can't reset a pending
    Lab request or clobber the real last-boot status."""
    decided: Mode | None = None
    decided_by: str | None = None
    requests: dict[str, str] = {}

    for selector in selectors:
        name = type(selector).__name__

        if decided is not None:
            requests[name] = REQUEST_NOT_CONSULTED
            continue

        try:
            requested = selector.mode_requested()
        except ModeSelectorUnavailable as exc:
            log.warning("%s unavailable (%s) -- skipping.", name, exc)
            requests[name] = REQUEST_UNAVAILABLE
            continue

        if requested is None:
            log.debug("%s has no preference.", name)
            requests[name] = REQUEST_NONE
            continue

        log.info("%s selects %s mode.", name, requested.value)
        if not dry_run:
            try:
                selector.clear()
            except ModeSelectorUnavailable as exc:
                log.error(
                    "%s: could not clear after consuming its request (%s) -- "
                    "reset it by hand.",
                    name, exc,
                )
        decided, decided_by = requested, name
        requests[name] = requested.value

    if decided is None:
        log.info("No selector expressed a preference -> Replication.")

    decision = ModeDecision(
        mode=decided or Mode.REPLICATION,
        decided_by=decided_by,
        selector_requests=requests,
    )
    log.info("Boot mode decided: %s", decision.summary())
    if dry_run:
        log.debug("Dry run -- not consuming the request or publishing the decision.")
    else:
        _publish(selectors, decision)
    return decision.mode


def _publish(selectors: Sequence[ModeSelector], decision: ModeDecision) -> None:
    for selector in selectors:
        try:
            selector.publish_result(decision)
        except Exception as exc:  # noqa: BLE001 -- a status sink must never break the boot
            log.warning(
                "%s: could not publish the boot decision (%s).",
                type(selector).__name__, exc,
            )
