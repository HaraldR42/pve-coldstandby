"""Find the standby-tagged VM backups on the NFS share.

The load-bearing rule of the whole design: the set of VMs to keep on
standby is derived *only* from data sitting on the NFS share, never from a
live query to `main`. Emergency mode is exactly the case where `main` is
gone, so anything that phones home to it is worthless when it matters.

Proxmox already gives us what we need for free: a `vzdump` archive embeds
the guest's full configuration at backup time, and `tags` is part of that
configuration. So we read the tag list straight out of each archive with
``vma config`` -- no restore, no source VM, no `main`.

Archive naming (file-level storage like NFS):

    vzdump-qemu-<vmid>-<YYYY_MM_DD>-<HH_MM_SS>.vma.zst

Only QEMU VMs are handled here by design (see README). LXC support would
add a `vzdump-lxc-*` branch that reads `./etc/vzdump/pct.conf` from the
tar.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

# Compression suffixes `vma` knows how to read. Order doesn't matter; the
# suffix is only used to recognise the file, `vma` sniffs the actual format.
_ARCHIVE_RE = re.compile(
    r"^vzdump-qemu-(?P<vmid>\d+)-"
    r"(?P<stamp>\d{4}_\d{2}_\d{2}-\d{2}_\d{2}_\d{2})"
    r"\.vma(?:\.(?:zst|gz|lzo))?$"
)
_STAMP_FMT = "%Y_%m_%d-%H_%M_%S"


@dataclasses.dataclass(frozen=True)
class BackupArchive:
    path: Path
    vmid: int
    taken_at: dt.datetime

    @property
    def name(self) -> str:
        return self.path.name


def _parse_archive_name(path: Path) -> BackupArchive | None:
    m = _ARCHIVE_RE.match(path.name)
    if not m:
        return None
    try:
        taken_at = dt.datetime.strptime(m.group("stamp"), _STAMP_FMT)
    except ValueError:
        log.warning("Archive %s has an unparseable timestamp -- skipping.", path.name)
        return None
    return BackupArchive(path=path, vmid=int(m.group("vmid")), taken_at=taken_at)


def _latest_per_vmid(archives: list[BackupArchive]) -> dict[int, BackupArchive]:
    latest: dict[int, BackupArchive] = {}
    for arc in archives:
        current = latest.get(arc.vmid)
        if current is None or arc.taken_at > current.taken_at:
            latest[arc.vmid] = arc
    return latest


def discover_archives(search_dir: Path) -> dict[int, BackupArchive]:
    """Return the most recent archive for each VMID found under ``search_dir``.

    `vzdump` on file storage drops archives in ``<storage>/dump/``. We
    search recursively so it doesn't matter whether the mount point is the
    storage root or the dump dir itself.
    """
    found: list[BackupArchive] = []
    for path in sorted(search_dir.rglob("vzdump-qemu-*")):
        if not path.is_file():
            continue
        arc = _parse_archive_name(path)
        if arc is not None:
            found.append(arc)

    latest = _latest_per_vmid(found)
    log.info(
        "Found %d QEMU archive(s) across %d VMID(s) under %s.",
        len(found),
        len(latest),
        search_dir,
    )
    return latest


def parse_vma_config(text: str) -> dict[str, str]:
    """Parse ``vma config`` output into a flat key/value dict.

    The output is the guest config: ``key: value`` lines, plus ``#qmdump#``
    comment lines we ignore. There are no snapshot sections in a backup's
    embedded config, so a flat parse is enough.
    """
    config: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        config[key.strip()] = value.strip()
    return config


def _read_embedded_config(archive: BackupArchive, cfg: Config) -> dict[str, str] | None:
    """Read the guest config embedded in an archive via ``vma config``.

    ``vma`` handles the zstd/gzip/lzo decompression itself given the file
    path. On any failure we return ``None`` and the caller drops the
    archive -- we never guess membership.
    """
    try:
        result = subprocess.run(
            ["vma", "config", str(archive.path)],
            capture_output=True,
            text=True,
            timeout=cfg.vma_config_timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not run `vma config %s`: %s", archive.name, exc)
        return None
    if result.returncode != 0:
        log.warning(
            "`vma config %s` failed (rc=%d): %s",
            archive.name,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    return parse_vma_config(result.stdout)


def config_has_tag(config: dict[str, str], tag: str) -> bool:
    raw = config.get("tags", "")
    tags = {t.strip() for t in re.split(r"[;,\s]+", raw) if t.strip()}
    return tag in tags


@dataclasses.dataclass(frozen=True)
class StandbyBackup:
    archive: BackupArchive
    config: dict[str, str]

    @property
    def vmid(self) -> int:
        return self.archive.vmid


def select_standby_backups(search_dir: Path, cfg: Config) -> list[StandbyBackup]:
    """The list of (latest archive, embedded config) to restore, sorted by VMID."""
    selected: list[StandbyBackup] = []
    for vmid, archive in sorted(discover_archives(search_dir).items()):
        embedded = _read_embedded_config(archive, cfg)
        if embedded is None:
            continue
        if not config_has_tag(embedded, cfg.standby_tag):
            log.debug("VM %d: latest archive not tagged %r -- skipping.", vmid, cfg.standby_tag)
            continue
        log.info(
            "VM %d: %s tagged %r, age %s.",
            vmid,
            archive.name,
            cfg.standby_tag,
            _humanize_age(archive.taken_at),
        )
        selected.append(StandbyBackup(archive=archive, config=embedded))
    return selected


def _humanize_age(taken_at: dt.datetime) -> str:
    delta = dt.datetime.now() - taken_at
    days = delta.days
    hours = delta.seconds // 3600
    return f"{days}d{hours}h"


def newest_archive_age_days(backups: list[StandbyBackup]) -> float | None:
    """Age in days of the *oldest* selected archive -- i.e. staleness of the
    weakest link. ``None`` if nothing was selected."""
    if not backups:
        return None
    oldest = min(b.archive.taken_at for b in backups)
    return (dt.datetime.now() - oldest).total_seconds() / 86400.0
