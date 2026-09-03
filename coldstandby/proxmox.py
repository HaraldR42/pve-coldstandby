"""Thin wrappers around the `qm` / `qmrestore` CLI on this node.

Everything here shells out to the same tools an operator would use by hand,
so behaviour is easy to reason about and to reproduce manually when
debugging. Nothing here talks to `main` or to the cluster -- this node is
standalone.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
import time
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

QEMU_CONF_DIR = Path("/etc/pve/qemu-server")


class ProxmoxError(RuntimeError):
    pass


def _run(
    cmd: list[str], *, dry_run: bool, timeout: float | None = None, check: bool = True
) -> None:
    """Run a mutating `qm`/`qmrestore` command, letting it log natively.

    Output is not captured: `qmrestore` progress and `qm` messages flow
    straight to our stdout and so into the journal, exactly as they would
    if an operator ran the command by hand. On failure the tool's own
    diagnostics are already in the journal; we just raise -- unless
    ``check`` is False (for best-effort steps like stopping a guest that is
    probably already stopped).
    """
    printable = " ".join(cmd)
    if dry_run:
        log.info("[dry-run] would run: %s", printable)
        return
    log.info("running: %s", printable)
    result = subprocess.run(cmd, timeout=timeout)
    if check and result.returncode != 0:
        raise ProxmoxError(f"command failed (rc={result.returncode}): {printable}")


# --------------------------------------------------------------------------
# Restore (Replication / Lab)
# --------------------------------------------------------------------------

def restore_vm(archive: Path, vmid: int, cfg: Config, *, dry_run: bool) -> None:
    """``qmrestore`` an archive over VMID, overwriting any existing guest.

    ``--force`` is required from the second weekly cycle onwards, when the
    VMID already exists locally from the previous restore. Identity is kept
    deliberately: no ``--unique``, so MAC addresses and the like match the
    original -- a standby that may have to *become* the original must not
    drift from it.
    """
    cmd = [
        "qmrestore",
        str(archive),
        str(vmid),
        "--force",
        "--storage",
        cfg.restore_storage,
    ]
    _run(cmd, dry_run=dry_run, timeout=cfg.restore_timeout_seconds)

    # pve-guests.service is masked on this node, so `onboot` is inert -- but
    # force it off anyway so that if the mask is ever lifted, a reboot does
    # not bring up a whole shadow copy of the environment unannounced.
    _run(["qm", "set", str(vmid), "--onboot", "0"], dry_run=dry_run)


def destroy_vm(vmid: int, *, dry_run: bool) -> None:
    """Destroy a VM and its disks.

    Used to remove a standby guest whose backup has vanished from the share
    (deleted on `main`, or the `standby` tag was dropped). Cold-standby
    guests are normally not running; the stop is best-effort and a "not
    running" result is fine. ``--purge`` also strips the VMID from any
    backup/replication job and Proxmox-HA config; ``--destroy-unreferenced-disks``
    sweeps disks the config forgot about.
    """
    _run(["qm", "stop", str(vmid)], dry_run=dry_run, check=False)
    _run(
        ["qm", "destroy", str(vmid), "--purge", "--destroy-unreferenced-disks", "1"],
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------
# Local VM config parsing (Emergency)
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class StartupSpec:
    order: int | None = None
    up_delay: int | None = None
    down_delay: int | None = None

    @property
    def sort_key(self) -> tuple[int, int]:
        # Unset order sorts *after* every explicit order, matching how
        # Proxmox's own `startall` treats it.
        return (0 if self.order is not None else 1, self.order or 0)


def parse_startup(value: str) -> StartupSpec:
    """Parse a ``startup`` config value like ``order=3,up=30,down=60``."""
    fields: dict[str, int] = {}
    for part in value.split(","):
        key, sep, raw = part.partition("=")
        if not sep:
            continue
        try:
            fields[key.strip()] = int(raw.strip())
        except ValueError:
            log.warning("Ignoring non-integer startup field %r in %r.", part, value)
    return StartupSpec(
        order=fields.get("order"),
        up_delay=fields.get("up"),
        down_delay=fields.get("down"),
    )


def parse_vm_conf(text: str) -> dict[str, str]:
    """Parse ``/etc/pve/qemu-server/<vmid>.conf`` -- current config only.

    Everything from the first ``[snapshot]`` header onwards is ignored; we
    only care about the guest's live configuration.
    """
    config: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            config[key.strip()] = value.strip()
    return config


@dataclasses.dataclass(frozen=True)
class LocalVM:
    vmid: int
    config: dict[str, str]

    @property
    def startup(self) -> StartupSpec:
        return parse_startup(self.config.get("startup", ""))

    def has_tag(self, tag: str) -> bool:
        raw = self.config.get("tags", "")
        return tag in {t.strip() for t in re.split(r"[;,\s]+", raw) if t.strip()}


def list_local_vms(conf_dir: Path = QEMU_CONF_DIR) -> list[LocalVM]:
    vms: list[LocalVM] = []
    for conf in sorted(conf_dir.glob("*.conf")):
        try:
            vmid = int(conf.stem)
        except ValueError:
            continue
        vms.append(LocalVM(vmid=vmid, config=parse_vm_conf(conf.read_text())))
    return vms


def ordered_standby_vms(cfg: Config, conf_dir: Path = QEMU_CONF_DIR) -> list[LocalVM]:
    """Standby-tagged local VMs in the order Emergency mode should start them.

    Sort: explicit ``order`` ascending, guests without an order last, ties
    broken by VMID ascending.
    """
    standby = [vm for vm in list_local_vms(conf_dir) if vm.has_tag(cfg.standby_tag)]
    return sorted(standby, key=lambda vm: (*vm.startup.sort_key, vm.vmid))


# --------------------------------------------------------------------------
# Start (Emergency)
# --------------------------------------------------------------------------

def start_vm(vmid: int, *, dry_run: bool) -> None:
    _run(["qm", "start", str(vmid)], dry_run=dry_run)


def start_in_order(vms: list[LocalVM], cfg: Config, *, dry_run: bool) -> None:
    """Start each VM, pausing between them by the guest's ``up`` delay
    (falling back to the configured default) so services that depend on an
    earlier guest -- a DC, a database -- are up before the next one boots.
    The delay after the final guest is skipped."""
    last = len(vms) - 1
    for i, vm in enumerate(vms):
        spec = vm.startup
        log.info(
            "Starting VM %d (order=%s).",
            vm.vmid,
            spec.order if spec.order is not None else "unset",
        )
        start_vm(vm.vmid, dry_run=dry_run)
        if i == last:
            break
        delay = spec.up_delay if spec.up_delay is not None else cfg.default_up_delay_seconds
        if delay > 0:
            log.info("Waiting %ds before next guest.", delay)
            if not dry_run:
                time.sleep(delay)
