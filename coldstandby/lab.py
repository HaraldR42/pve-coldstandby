"""Lab mode -- deliberately entered, stays up for hands-on work.

Someone set the online lab selector before this boot; mode resolution has
already consumed that flag (it's back to "replication" now), so the *next*
unattended boot is normal again.

Lab mode does **not** touch the backup share and does **not** restore
anything -- it leaves local disk exactly as the last Replication cycle left
it and just stays powered on. Work against those guests as they are; start
whichever ones you need by hand. To refresh them from backup first, run
``--replicate-now`` (see main.py). Reboot or flip the selector back to
Replication when done.
"""
from __future__ import annotations

import logging

from .config import Config
from .proxmox import list_local_vms

log = logging.getLogger(__name__)


def run(cfg: Config, *, dry_run: bool) -> int:
    standby = [vm for vm in list_local_vms() if vm.has_tag(cfg.standby_tag)]
    log.info(
        "Lab mode: no refresh performed. %d standby-tagged guest(s) on local "
        "disk from the last Replication cycle, left stopped. Node stays up; "
        "start guests manually, reboot when finished.",
        len(standby),
    )
    return 0
