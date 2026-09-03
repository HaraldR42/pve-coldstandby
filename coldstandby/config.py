"""Central configuration for the cold-standby controller.

Configuration is read from a JSON file (default: /etc/coldstandby/config.json)
rather than hardcoded, so the secrets (the dongle marker token, the online
selector's access token) never end up committed to source control. See
config.example.json for the expected shape -- copy it to the real path and
fill it in.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("COLDSTANDBY_CONFIG", "/etc/coldstandby/config.json"))


@dataclass
class Config:
    # --- Online lab selector (optional) ------------------------------
    # An online switch whose only job is to push the *next* boot into Lab
    # mode without a dongle. The reference client
    # (selectors/home_assistant.py) talks to a Home Assistant input_select.
    # Leave ha_base_url / ha_token empty
    # to disable this layer entirely -- resolution then goes dongle ->
    # Replication.
    ha_base_url: str = ""
    ha_token: str = ""
    ha_lab_select_entity: str = "input_select.coldstandby_mode"
    ha_lab_option: str = "lab"
    ha_replication_option: str = "replication"
    ha_timeout_seconds: float = 5.0

    # --- Dongle mode selection ---------------------------------------
    # A dongle names its mode in the fs label: "<prefix>-EMERGENCY",
    # "<prefix>-LAB", "<prefix>-REPLICATION". To count it must also carry a
    # marker file with this exact token -- a label match alone is never
    # enough (see selectors/dongle.py).
    dongle_label_prefix: str = "COLDSTANDBY"
    dongle_marker_filename: str = ".coldstandby-token"
    dongle_marker_token: str = ""  # required: set a random secret in config.json
    dongle_mount_point: Path = field(default_factory=lambda: Path("/mnt/coldstandby-dongle"))

    # --- NFS backup share on `filer` -----------------------------------
    nfs_server: str = "filer.roelle.home"
    nfs_export: str = "/export/pve-backups"
    nfs_mount_point: Path = field(default_factory=lambda: Path("/mnt/coldstandby-nfs"))

    # --- Tagging ---------------------------------------------------------
    standby_tag: str = "standby"

    # --- Restore (Replication / Lab) ----------------------------------
    # Local storage the restored VM disks land on. Must exist on this node
    # (`pvesm status`) and be big enough for every standby-tagged guest.
    restore_storage: str = "local-lvm"
    restore_timeout_seconds: float = 3600.0  # per VM; a full .vma restore is not incremental
    vma_config_timeout_seconds: float = 60.0

    # --- Orphan cleanup (Replication) -------------------------------
    # Before restoring, destroy any locally-present standby-tagged VM that
    # no longer has a backup on the share -- deleted on `main`, or the
    # `standby` tag was removed. Without this the backup node slowly fills
    # with guests that Emergency mode would then start. Set False to only
    # ever add/refresh, never remove.
    remove_orphans: bool = True
    # Safety fence for the above: orphan removal is confined to this
    # inclusive VMID range, and a standby-tagged VM outside it is left alone
    # with a warning. Keep standby guests renumbered into this range on
    # `main`. Set to "" to disable the fence (any tagged orphan is fair
    # game).
    standby_vmid_range: str = "1000-1999"

    # --- Emergency start ordering ------------------------------------
    # Used between guests that have no explicit `up=` in their `startup=`.
    default_up_delay_seconds: int = 30

    # --- Replication mode behaviour ---------------------------------
    # After a successful weekly refresh, power this node back off. Waking it
    # is someone else's job (WOL, external to this project); the node ends
    # its own day. Suppressed by --dry-run and by --no-shutdown.
    shutdown_after_replication: bool = True

    # Log a warning if the oldest selected archive is older than this. A
    # once-a-week job that silently stops should be noticed within days,
    # not a month.
    staleness_warn_days: float = 8.0

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> "Config":
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raise SystemExit(
                f"Config file not found at {path}. "
                f"Copy config.example.json there and fill in the secrets."
            )
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Config file at {path} is not valid JSON: {exc}")

        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise SystemExit(f"Unknown config key(s) in {path}: {', '.join(sorted(unknown))}")

        for path_field in ("dongle_mount_point", "nfs_mount_point"):
            if path_field in raw:
                raw[path_field] = Path(raw[path_field])

        cfg = cls(**raw)

        if not cfg.dongle_marker_token:
            raise SystemExit(
                "dongle_marker_token is empty in config.json -- set it to a "
                "random secret before this can safely honour a mode dongle."
            )

        # Validate the fence eagerly so a typo fails at load, not mid-run
        # right before a destroy.
        cfg.orphan_vmid_bounds()

        if bool(cfg.ha_base_url) != bool(cfg.ha_token):
            raise SystemExit(
                "ha_base_url and ha_token must be set together (or both left "
                "empty to disable the online lab selector)."
            )

        return cfg

    @property
    def online_selector_enabled(self) -> bool:
        """Whether the optional online lab selector is configured."""
        return bool(self.ha_base_url and self.ha_token)

    def orphan_vmid_bounds(self) -> tuple[int, int] | None:
        """Parse ``standby_vmid_range`` into an inclusive (low, high) pair,
        or ``None`` if unset."""
        spec = self.standby_vmid_range.strip()
        if not spec:
            return None
        low, sep, high = spec.partition("-")
        try:
            lo, hi = int(low), int(high)
        except ValueError:
            sep = ""  # force the error below
            lo = hi = 0
        if not sep or lo > hi:
            raise SystemExit(
                f"standby_vmid_range must be 'LOW-HIGH' with LOW <= HIGH, got "
                f"{self.standby_vmid_range!r}."
            )
        return lo, hi
