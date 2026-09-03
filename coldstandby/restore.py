"""Refresh-from-backups logic for Replication mode.

Mount the backup share read-only, work out which VMs are standby-tagged
*from the archives themselves*, remove any local standby guest whose backup
has since disappeared, then ``qmrestore --force`` the latest of each. What
happens after the refresh -- Replication powers the node off -- lives in
the mode handler, not here.

Only Replication refreshes. Lab mode deliberately does not: it works
against whatever the last Replication cycle left on local disk.
"""
from __future__ import annotations

import dataclasses
import logging

from .backups import newest_archive_age_days, select_standby_backups
from .config import Config
from .nfs import mounted_backup_share
from .proxmox import ProxmoxError, destroy_vm, list_local_vms, restore_vm

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RefreshResult:
    restored: list[int] = dataclasses.field(default_factory=list)
    removed: list[int] = dataclasses.field(default_factory=list)
    failed: list[int] = dataclasses.field(default_factory=list)
    oldest_age_days: float | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and bool(self.restored)

    def summary(self) -> str:
        parts = [f"restored={len(self.restored)}"]
        if self.removed:
            parts.append(f"removed={sorted(self.removed)}")
        if self.failed:
            parts.append(f"FAILED={sorted(self.failed)}")
        if self.oldest_age_days is not None:
            parts.append(f"oldest={self.oldest_age_days:.1f}d")
        return " ".join(parts)


def refresh_standby_vms(cfg: Config, *, dry_run: bool) -> RefreshResult:
    result = RefreshResult()

    with mounted_backup_share(cfg, dry_run=dry_run) as share:
        backups = select_standby_backups(share, cfg)
        result.oldest_age_days = newest_archive_age_days(backups)

        if not backups:
            # Refuse to interpret an empty/unreadable share as "main has no
            # standby VMs any more" -- that would delete the entire standby
            # set on a transient NFS hiccup. Do nothing.
            log.warning(
                "No archives tagged %r found on the share -- nothing to "
                "restore, and skipping orphan cleanup.",
                cfg.standby_tag,
            )
            return result

        wanted = {b.vmid for b in backups}
        if cfg.remove_orphans:
            _remove_orphans(cfg, wanted, result, dry_run=dry_run)

        for backup in backups:
            vmid = backup.vmid
            try:
                log.info("Restoring VM %d from %s ...", vmid, backup.archive.name)
                restore_vm(backup.archive.path, vmid, cfg, dry_run=dry_run)
                result.restored.append(vmid)
            except (ProxmoxError, OSError) as exc:
                # One bad archive must not stop the rest -- a partial
                # standby is better than none.
                log.error("Restore of VM %d failed: %s", vmid, exc)
                result.failed.append(vmid)

    log.info("Refresh complete: %s", result.summary())
    return result


def _remove_orphans(
    cfg: Config, wanted: set[int], result: RefreshResult, *, dry_run: bool
) -> None:
    """Destroy local standby-tagged VMs that have no backup in ``wanted``."""
    bounds = cfg.orphan_vmid_bounds()
    for vm in list_local_vms():
        if vm.vmid in wanted or not vm.has_tag(cfg.standby_tag):
            continue
        if bounds and not (bounds[0] <= vm.vmid <= bounds[1]):
            log.warning(
                "VM %d is standby-tagged with no backup on the share, but is "
                "outside standby_vmid_range %s -- leaving it alone.",
                vm.vmid,
                cfg.standby_vmid_range,
            )
            continue
        try:
            log.info(
                "VM %d: standby-tagged locally but no backup on the share -- "
                "removing.",
                vm.vmid,
            )
            destroy_vm(vm.vmid, dry_run=dry_run)
            result.removed.append(vm.vmid)
        except (ProxmoxError, OSError) as exc:
            log.error("Could not remove orphan VM %d: %s", vm.vmid, exc)
            result.failed.append(vm.vmid)
