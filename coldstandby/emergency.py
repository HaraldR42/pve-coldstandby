"""Emergency mode -- `main` is presumed dead; bring the standby up for real.

Normally entered only via the physical `CSBY-EMERG` dongle (see
selectors/dongle.py) -- so it cannot be triggered remotely or by accident, and it
works with the rest of the home network down. (`--force-mode emergency` is
the recovery-only bypass.)

This mode does NOT touch the NFS share or restore anything. It runs against
whatever the last Replication cycle already left on local disk. It reads
the standby-tagged local VM configs, works out the start order from each
guest's ``startup=`` line, and starts them one at a time with the
configured delay between -- so a domain controller or database is serving
before the things that depend on it boot.

This mode is only ever the tail end of a manual procedure (see the README
"Emergency failover" section). The steps this code cannot enforce, and
which matter most:

  1. Confirm `main` is actually dead and not about to recover.
  2. Physically pull `main`'s network uplink(s) -- a manual STONITH, so a
     half-alive `main` can't serve the same IPs / AD DC / shared state
     while the standby runs. Only then plug in the dongle and power this
     node on.

Two live copies of a single-master service (the AD DC especially) both
taking writes is the one mistake here that is genuinely painful to undo.
"""
from __future__ import annotations

import logging

from .config import Config
from .proxmox import ProxmoxError, ordered_standby_vms, start_in_order

log = logging.getLogger(__name__)


def run(cfg: Config, *, dry_run: bool) -> int:
    vms = ordered_standby_vms(cfg)
    if not vms:
        log.error(
            "Emergency mode but no standby-tagged VMs on local disk. Has a "
            "Replication cycle ever run on this node?"
        )
        return 1

    order = ", ".join(
        f"{vm.vmid}(order={vm.startup.order if vm.startup.order is not None else '-'})"
        for vm in vms
    )
    log.info("Emergency start order: %s", order)

    try:
        start_in_order(vms, cfg, dry_run=dry_run)
    except ProxmoxError:
        log.exception("A guest failed to start; remaining guests not started.")
        return 1

    log.info("All %d standby guest(s) started. Node stays up.", len(vms))
    return 0
