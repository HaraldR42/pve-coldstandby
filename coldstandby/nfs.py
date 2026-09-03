"""Read-only NFS mount of the backup share on `filer`.

Deliberately minimal: a context manager that mounts the share read-only for
the duration of a `with` block and unmounts it afterwards. Two safety
properties matter here:

* The mount is always ``ro`` -- this node must never be able to mutate the
  backup archives, even by accident. The export on `filer` is also
  host-restricted to read-only (belt and suspenders), but we don't rely on
  that being configured correctly.
* If the share is already mounted when we start (e.g. a previous run was
  killed before it could clean up), we reuse it and leave it alone on exit
  rather than yanking a mount we didn't create.
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Iterator

from .config import Config

log = logging.getLogger(__name__)

# ``soft`` + a bounded timeout so a dead `filer` makes us fail fast instead
# of hanging an unattended boot forever. ``nolock`` because we only ever
# read. ``noexec,nosuid,nodev`` because nothing on that share should ever be
# treated as executable or special.
_MOUNT_OPTIONS = "ro,soft,timeo=100,retrans=2,nolock,noexec,nosuid,nodev"


def _is_mounted(mount_point: Path) -> bool:
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "TARGET", str(mount_point)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@contextlib.contextmanager
def mounted_backup_share(cfg: Config, dry_run: bool = False) -> Iterator[Path]:
    """Yield the path at which the backup share is mounted read-only."""
    target = cfg.nfs_mount_point
    source = f"{cfg.nfs_server}:{cfg.nfs_export}"

    if dry_run:
        log.info("[dry-run] would mount %s at %s (%s)", source, target, _MOUNT_OPTIONS)
        yield target
        return

    if _is_mounted(target):
        log.info("%s already mounted -- reusing it, will not unmount on exit.", target)
        yield target
        return

    target.mkdir(parents=True, exist_ok=True)
    log.info("Mounting %s at %s read-only.", source, target)
    mount = subprocess.run(
        ["mount", "-t", "nfs", "-o", _MOUNT_OPTIONS, source, str(target)],
        capture_output=True,
        text=True,
    )
    if mount.returncode != 0:
        raise RuntimeError(
            f"Could not mount backup share {source} at {target}: {mount.stderr.strip()}"
        )

    try:
        yield target
    finally:
        umount = subprocess.run(
            ["umount", str(target)], capture_output=True, text=True
        )
        if umount.returncode != 0:
            # Not fatal -- log it so it gets noticed, but a lingering
            # read-only mount can't hurt anything.
            log.warning("Could not unmount %s: %s", target, umount.stderr.strip())
        else:
            log.info("Unmounted %s.", target)
