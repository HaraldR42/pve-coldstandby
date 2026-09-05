"""`DongleSelector` -- mode selection from a labelled, token-marked USB stick.

A dongle names the mode it selects in its filesystem label (with the
default ``dongle_label_prefix`` of ``CSBY``):

    CSBY-EMERG   CSBY-LAB   CSBY-REPL

Filesystem labels are short -- 16 chars on ext, only 11 on vfat -- so the
mode part is a fixed short code (see ``mode.DONGLE_LABEL_CODES``), not the
spelled-out mode name. To count, the dongle must ALSO carry a marker file
(``dongle_marker_filename``) whose contents exactly match
``dongle_marker_token`` -- a label match alone is never enough, so a
random stick that happens to share a label can't trigger anything.

Fail-closed by construction: any ambiguity (missing/extra label, bad or
missing marker, unmountable device, *more than one distinct mode dongle
plugged in at once*) resolves to "no opinion" -- never to a guessed mode.

Entirely local: no network, nothing online. This is the selector that must
keep working precisely when the rest of the home network is what's down,
which is why `main` always gives it top priority.
"""
from __future__ import annotations

import logging
import subprocess

from ..config import Config
from ..mode import Mode, ModeSelector, dongle_label

log = logging.getLogger(__name__)


class DongleSelector(ModeSelector):
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def mode_requested(self) -> Mode | None:
        matched: list[Mode] = []
        for mode in Mode:
            label = dongle_label(self._cfg.dongle_label_prefix, mode)
            device = self._find_device_by_label(label)
            if device is None:
                continue
            if self._marker_token_matches(device):
                log.info("Valid %s dongle found (%s).", mode.value, label)
                matched.append(mode)

        if not matched:
            return None
        if len(matched) > 1:
            log.error(
                "Dongles for multiple modes present (%s) -- ignoring all of them.",
                [m.value for m in matched],
            )
            return None
        return matched[0]

    # clear(): a dongle is stateless -- you remove it by hand. Inherit the
    # base no-op.

    @staticmethod
    def _find_device_by_label(label: str) -> str | None:
        try:
            result = subprocess.run(
                ["blkid", "--label", label], capture_output=True, text=True
            )
        except OSError as exc:
            log.warning("Could not run blkid (%s) -- treating as no dongle.", exc)
            return None
        devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(devices) == 0:
            return None
        if len(devices) > 1:
            log.warning(
                "Multiple devices with label %r (%s) -- ambiguous, ignoring.",
                label, devices,
            )
            return None
        return devices[0]

    def _marker_token_matches(self, device: str) -> bool:
        """Mount ``device`` read-only, check the marker token, unmount."""
        cfg = self._cfg
        cfg.dongle_mount_point.mkdir(parents=True, exist_ok=True)
        try:
            mount = subprocess.run(
                ["mount", "-o", "ro,noexec,nosuid,nodev", device, str(cfg.dongle_mount_point)],
                capture_output=True, text=True,
            )
        except OSError as exc:
            log.warning("Could not run mount (%s) -- treating dongle %s as absent.", exc, device)
            return False
        if mount.returncode != 0:
            log.warning("Candidate dongle %s could not be mounted: %s", device, mount.stderr.strip())
            return False

        try:
            marker = cfg.dongle_mount_point / cfg.dongle_marker_filename
            try:
                content = marker.read_text().strip()
            except OSError:
                log.warning("Dongle %s mounted but marker file %s missing/unreadable.", device, marker.name)
                return False
            if content != cfg.dongle_marker_token:
                log.warning("Dongle %s marker token did not match -- ignoring.", device)
                return False
            return True
        finally:
            subprocess.run(["umount", str(cfg.dongle_mount_point)], capture_output=True)
