"""Entry point for the cold-standby controller.

Run at boot, after network-online.target, as coldstandby.service
(see systemd/coldstandby.service). pve-guests.service is masked
permanently on this node -- guest starts happen only through this
controller's own ordering logic in Emergency mode.

Everything this controller does is logged to the systemd journal (terse
format -- journald supplies the timestamp and the `coldstandby`
identifier). Inspect a run with `journalctl -u coldstandby`. The optional
online selector is only consulted to resolve the boot mode; nothing
reports to it.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from . import emergency, lab, replication
from .config import DEFAULT_CONFIG_PATH, Config
from .mode import Mode, determine_mode
from .selectors import build_selectors

log = logging.getLogger("coldstandby")

PVE_GUESTS_UNIT = "pve-guests.service"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cold-standby boot-mode controller")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.json (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intended actions without executing them (never shuts down).",
    )
    parser.add_argument(
        "--no-shutdown",
        action="store_true",
        help="Replication mode: skip the final poweroff even on success.",
    )
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument(
        "--force-mode",
        choices=[m.value for m in Mode],
        help="Skip resolution and run this mode. For testing/recovery only.",
    )
    trigger.add_argument(
        "--replicate-now",
        action="store_true",
        help="Run a replication refresh right now and stop -- no mode "
        "resolution, nothing online consulted, and never a shutdown. For "
        "refreshing the standby disks by hand while the node is already up.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _setup_logging(verbose: bool) -> None:
    # Terse on purpose: under systemd this goes to the journal, which adds
    # its own timestamp and the SyslogIdentifier. `journalctl -u coldstandby`
    # is the native way to read it.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _enforce_pve_guests_masked(dry_run: bool) -> None:
    """Re-assert that pve-guests.service is masked, on every run, every mode.

    Guest starts on this node happen only through this controller's
    Emergency-mode ordering -- never through Proxmox's native
    onboot/startall. If pve-guests.service is ever left unmasked (a PVE
    package upgrade recreating it, a hand-run `systemctl unmask`, a
    reinstall), the next ordinary boot would `startall` a shadow copy of
    the whole environment against `main`. Re-masking here makes that a
    self-healing condition instead of a latent trap.
    """
    try:
        state = subprocess.run(
            ["systemctl", "is-enabled", PVE_GUESTS_UNIT],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError as exc:
        # Defense-in-depth, not the core job -- don't abort the run over it.
        log.error("Could not check %s state (%s); continuing.", PVE_GUESTS_UNIT, exc)
        return

    if state == "masked":
        log.debug("%s already masked.", PVE_GUESTS_UNIT)
        return

    if dry_run:
        log.warning("[dry-run] %s is %r -- would mask it.", PVE_GUESTS_UNIT, state or "unknown")
        return

    log.warning("%s is %r, not masked -- masking it now.", PVE_GUESTS_UNIT, state or "unknown")
    try:
        result = subprocess.run(
            ["systemctl", "mask", PVE_GUESTS_UNIT], capture_output=True, text=True
        )
    except OSError as exc:
        log.error("Could not mask %s (%s); continuing.", PVE_GUESTS_UNIT, exc)
        return
    if result.returncode != 0:
        log.error("Could not mask %s: %s", PVE_GUESTS_UNIT, result.stderr.strip())
    else:
        log.warning(
            "%s masked. If this boot had already started it, check for "
            "unexpectedly running guests.",
            PVE_GUESTS_UNIT,
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    cfg = Config.load(args.config)
    _enforce_pve_guests_masked(args.dry_run)

    if args.replicate_now:
        log.info("Manual replication refresh%s.", " (dry run)" if args.dry_run else "")
        try:
            return replication.run(cfg, dry_run=args.dry_run, allow_shutdown=False)
        except Exception:
            log.exception("Unhandled error during manual replication refresh.")
            return 1

    if args.force_mode:
        mode = Mode(args.force_mode)
        log.warning("Mode resolution skipped -- forced to %s.", mode.value)
    else:
        mode = determine_mode(build_selectors(cfg))

    log.info("Boot mode: %s%s", mode.value, " (dry run)" if args.dry_run else "")

    try:
        if mode is Mode.REPLICATION:
            return replication.run(
                cfg, dry_run=args.dry_run, allow_shutdown=not args.no_shutdown
            )
        if mode is Mode.LAB:
            return lab.run(cfg, dry_run=args.dry_run)
        return emergency.run(cfg, dry_run=args.dry_run)
    except Exception:
        # A non-zero exit marks the unit failed; the traceback is in the
        # journal. That is the whole failure signal -- no external sink.
        log.exception("Unhandled error while executing %s mode.", mode.value)
        return 1


if __name__ == "__main__":
    sys.exit(main())
