"""Replication mode -- the default, unattended weekly path.

Wake (via WOL, driven externally) -> refresh every standby-tagged VM from
its latest backup -> power back off. It overwrites this node's local guest
copies each run (`qmrestore --force`) -- but that is the only thing it can
affect: it never touches `main`, the cluster, the read-only backup
archives, or anything running elsewhere, it starts no guests, and it shuts
the node down when done. That bounded blast radius is why it's also the
fallback whenever mode resolution is uncertain.

Guests are restored but NOT started. This node is a cold spare; the disks
just need to be current. Progress and outcome go to the journal
(`journalctl -u coldstandby`); a failed run leaves the unit in a failed
state and the node powered on.
"""
from __future__ import annotations

import logging
import subprocess

from .config import Config
from .restore import refresh_standby_vms

log = logging.getLogger(__name__)


def run(cfg: Config, *, dry_run: bool, allow_shutdown: bool) -> int:
    result = refresh_standby_vms(cfg, dry_run=dry_run)

    if (
        result.oldest_age_days is not None
        and result.oldest_age_days > cfg.staleness_warn_days
    ):
        log.warning(
            "Oldest standby archive is %.1f days old (> %.1f) -- is the weekly "
            "backup job on `main` still running?",
            result.oldest_age_days,
            cfg.staleness_warn_days,
        )

    if not result.ok:
        # Something went wrong or nothing was found -- stay up so an
        # operator can look, don't hide the evidence by powering off.
        log.error("Replication did not complete cleanly (%s) -- node stays on.", result.summary())
        return 1

    log.info("Replication OK: %s", result.summary())
    _maybe_shutdown(cfg, dry_run=dry_run, allow_shutdown=allow_shutdown)
    return 0


def _maybe_shutdown(cfg: Config, *, dry_run: bool, allow_shutdown: bool) -> None:
    if not cfg.shutdown_after_replication:
        log.info("shutdown_after_replication is off -- staying powered on.")
        return
    if not allow_shutdown:
        log.info("--no-shutdown given -- staying powered on.")
        return
    if dry_run:
        log.info("[dry-run] would power the node off now.")
        return
    log.info("Powering off.")
    subprocess.run(["systemctl", "poweroff"], check=False)
