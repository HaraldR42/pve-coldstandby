# pve-coldstandby

Boot-mode controller for a cold standby backup Proxmox VE node. On every boot it
resolves which of three modes to run in, then does that mode's work and
gets out of the way.

The backup node is a cold spare: normally powered off, woken roughly weekly
(WOL, driven from outside this project), refreshed from `main`'s backups on
an NFS share, and shut back down. If `main` dies, plugging in a physical
dongle turns the same node into a running replacement.

## Motivation & design

A two-machine home lab: one always-on Proxmox host (`main`) and a second
machine of similar capability, kept powered off to save energy, that
should be able to take over if `main` dies — a cold standby, without a
second always-on box and without a babysitting chore.

### Design criteria

Goals, in rough priority order. These are the *what* and *why*; how they
are met is the rest of this document.

1. **The routine path is fully unattended.** Keeping the standby current
   must cost zero human attention. A home lab that needs weekly upkeep to
   stay recoverable won't stay recoverable.
2. **Failover is a deliberate human decision, never automatic.** Nothing
   watches `main` or promotes the standby on its own. A wrong automatic
   call during a brief outage — bringing up a second copy of a
   single-master service — is far worse than waiting for a person to
   assess the situation.
3. **Promotion to a live replacement takes a deliberate physical act.**
   It risks split-brain, so it must not be possible remotely, across the
   network, or through a setting someone forgot to change — only by
   someone physically at the machines who has first isolated `main`.
4. **One source of truth for what is protected.** The set of guests the
   standby covers is decided in exactly one place — on `main`, as part of
   each guest's own definition — never kept as a second list elsewhere
   that could drift out of sync.
5. **The standby pulls; `main` never pushes.** `main` does its normal
   backups and is otherwise unaware the standby exists — nothing on `main`
   reaches into it, triggers it, or depends on it. The standby drives its
   own cycle and can be rebuilt or left off for a month without `main`
   caring.
6. **Ambiguity resolves to the harmless state.** Anything unclear — a
   component unreachable, an unexpected value, incomplete data — must land
   on behaviour that changes nothing outside the standby and then stops,
   never on bringing guests up.
7. **The failover path survives the network being gone.** Deciding to
   fail over and bringing the guests up must not depend on `main`, name
   resolution, the LAN, or any other host — the situations that call for
   failover are exactly the ones where those are unavailable.
8. **Few moving parts.** No clustering, no quorum, no replication
   protocol, no second always-on machine, no new service on `main`.
   Prefer reusing infrastructure that already exists.
9. **Failover is fast.** Recovery should be starting guests that are
   already present, not waiting on a copy or a restore.

Accepted trade-offs: the recovery point is only as fresh as the last
routine refresh (up to a week old); failing *back* to `main` afterwards is
a manual step; keeping the two machines' network configuration in sync is
a manual responsibility; a refresh copies whole disks, so its duration
scales with total size, not with how much changed.

### The setup

`main` runs ~half a dozen VMs and already takes a weekly `vzdump` backup of
the important ones onto an NFS share. The NFS server (`filer`) is itself
**a VM on `main`** — so the backup share is reachable exactly when `main`
is healthy, and is gone the moment `main` isn't. That single fact shapes
the design:

- **Replication** and **Lab** run while `main` is up, so they can rely on
  the share.
- **Emergency** runs when `main` is (presumed) dead, so `filer` and the
  share are dead too. Emergency therefore touches neither — it works only
  from what the last Replication run already copied to local disk. `filer`
  is not a component of the failover path; it's just where backups land.

That weekly backup job is the only data movement in the design; everything
here is built around consuming it.

### Approaches considered and skipped

- **Proxmox Datacenter Manager (PDM).** Purpose-built for managing
  standalone nodes without clustering, GA since late 2025 — but as of
  1.1.x its strength is centralized visibility and on-demand migration.
  Scheduled backup integration and an unattended "sync weekly to a
  powered-off box" workflow are still roadmap, not shipped.
- **Native ZFS replication (`pvesr`) + a QDevice.** Proxmox's built-in
  answer: incremental block-level ZFS `send`/`receive`, GUI-managed,
  fastest possible failover. Rejected for moving parts: it only works
  between nodes of the *same cluster*, and a 2-node cluster with one node
  usually off loses quorum. The usual fix — a `corosync-qnetd` QDevice on
  a third always-on host — isn't available here: the only other machine
  (`filer`) is a VM on `main` and dies with it. That leaves ZFS on both
  nodes plus permanent cluster membership for a machine that's off 95% of
  the time, for no quorum safety net.
- **Hand-rolled `zfs send | ssh | zfs receive` on a timer.** Drops the
  cluster, keeps the speed. Rejected because you then own snapshot
  retention, incremental-chain bookkeeping and copying guest `.conf` files
  by hand — reimplementing, with less hardening, what `vzdump` already
  does — and it still forces ZFS.
- **Proxmox Backup Server on the standby node.** Nice versioned history
  and dirty-bitmap incrementals. Rejected because recovery is not instant:
  every guest must be restored from the datastore onto local disk before
  it can run, which for half a dozen VMs is real downtime — the opposite
  of "failover is fast". (This design still *restores* from backups, but
  does it ahead of time, so an emergency is just a start.)
- **A network-reachable switch as the mechanism for forcing modes.** Fine
  as a *convenience* for the harmless case (see the online lab selector
  below), but it can never be the thing failover depends on: the moment
  you most need to force a promotion is exactly when the network is down.
  That's why the physical dongle is checked first and works entirely
  offline.
- **Encoding the mode in the Wake-on-LAN packet, or a
  microcontroller/relay "virtual dongle".** The WOL SecureOn password
  isn't visible to the booted OS, so there's nothing to read back. A
  dedicated MCU is new hardware and firmware to maintain for a rare
  manual action — a labelled USB stick does the same job with nothing to
  keep patched.
- **IPMI/BMC next-boot override.** The cleanest option *if the hardware
  has a BMC* — fully out-of-band, survives any LAN/DNS/NFS failure.
  Consumer-grade hardware often doesn't have one, so it can't be the
  mechanism the design depends on; where present it's a fine addition
  alongside the dongle.

## Modes

Every boot runs in one of three modes. Which one is decided by asking a
priority-ordered list of **mode selectors** (`ModeSelector` in `mode.py`)
in turn — the first that expresses a preference wins; if none do, the
answer is Replication. A selector that can't be consulted (unreachable,
garbled answer) is skipped, not fatal. Once the mode is settled the
outcome is handed back to *every* selector (`publish_result`) — so it can
be surfaced somewhere (the Home Assistant selector writes it to a status
entity); a dry run skips this.

Two selectors ship:

1. **Dongle** (`DongleSelector`, always first, always present). A valid
   mode dongle plugged in selects the mode, beating everything after it. A
   dongle names its mode in its filesystem label
   (`COLDSTANDBY-EMERGENCY`, `COLDSTANDBY-LAB`, `COLDSTANDBY-REPLICATION`)
   and must also carry a marker file whose contents match a secret token
   in config — a label alone does nothing. Decided purely from local
   hardware: nothing online, so it works when the home network is what's
   down. Two different mode dongles at once are refused (no preference —
   falls through).
2. **Online selector** (`HomeAssistantSelector`, optional). A switch —
   backed here by a Home Assistant `input_select` — that requests Lab, and
   nothing else, for the next boot (and self-resets once consumed, so a
   forgotten flag can't re-trigger Lab). A convenience for choosing Lab
   from your phone without walking over with a dongle; never involved in
   an emergency. Not configured, or unreachable → no preference.

Add another mechanism (MQTT, a REST endpoint, a flag file …) by dropping a
`ModeSelector` subclass into the `coldstandby/selectors/` package and
registering it in `selectors.build_selectors` — see that package's
docstring.

The modes themselves:

- **Emergency** — `main` is presumed dead. Reads the standby-tagged VM
  configs already on local disk, works out start order from each guest's
  `startup=` line, and starts them one at a time with the configured delay
  between. Does **not** touch NFS. Never entered automatically — there is
  no `main`-down detector anywhere in this project. It is the tail end of a
  deliberate operator procedure (**Emergency failover** below).
- **Lab** — does **not** touch the backup share and does **not** restore
  anything; leaves local disk as the last Replication cycle left it and
  just stays powered on for hands-on work. Guests are left stopped; start
  them by hand. To bring them current first, run a manual replication
  refresh (`--replicate-now`, see below).
- **Replication** — the default, and the fallback whenever resolution is
  uncertain (online selector unreachable, an unexpected value, …). Mounts
   the NFS share read-only, destroys any local standby-tagged VM that no longer
   has a backup on the share (deleted on `main`, or untagged — see *Orphan
   cleanup* below), restores the latest backup of every standby-tagged VM
   (`qmrestore --force`), then powers the node off. It *does* overwrite and
   delete this node's local guest copies every run — but that's the whole
   point, and it's the only thing it can hurt: it never touches `main`, the
   cluster, the backup archives, or anything running elsewhere, and it
   starts no guests and shuts itself down. That bounded blast radius is
   why it's the safe default when we're not sure what to do.

## Emergency failover (manual)

There is no automatic failover. Nothing in this project watches `main` or
decides it is dead. Promoting the standby to a live replacement is a
hands-on procedure a human runs, in this order:

1. **Decide `main` is actually gone** and not about to recover on its own.
   This is a judgement call — for a single-master service like an AD DC,
   getting it wrong is expensive to undo.
2. **Fence `main` — physically pull its network uplink(s).** This is a
   manual STONITH: a half-alive `main` (mid-boot, degraded, link
   flapping) must not be able to serve the guests' IPs, answer as the AD
   DC, or touch shared state while the standby is about to do the same.
   Cutting the wire is crude but certain, and needs no cooperation from
   the sick host. Do **not** skip this even if `main` "looks dead".
3. **Plug the `COLDSTANDBY-EMERGENCY` dongle into the backup node.**
4. **Power the backup node on** (WOL or the front-panel button). It boots,
   resolves Emergency mode from the dongle, starts the standby guests in
   `startup=` order, and stays up.

The environment now runs on the backup node. `main` stays powered off and
unplugged until you deliberately fail back.

**Failing back** is also fully manual and is not automated here: stop the
standby guests, remove the dongle (so the next boot is normal), bring
`main` back and reconcile any state that diverged while it was down, only
then reconnect its network, and run one Replication cycle so the weekly
job is healthy again.

## What talks to what

- **Mode selectors** are the pluggable mechanisms for choosing the boot
  mode. `mode.py` defines the interface — `ModeSelector`, with
  `mode_requested() -> Mode | None`, `clear()` (consume a one-shot
  request), and `publish_result(decision)` (report the outcome) — plus
  `ModeSelectorUnavailable` and `determine_mode`. The `selectors/` package
  holds the implementations and `build_selectors`, which fixes their
  priority order.
- **The dongle** (`DongleSelector`) is the manual override: any mode,
  decided from local hardware only, first in the list so it beats
  everything. A Lab/Emergency dongle consumes nothing — pull it out and
  the override is gone. Only a valid marker token counts. `publish_result`
  is a no-op — a USB stick has nothing to report to.
- **The online selector** (`HomeAssistantSelector`) is an optional
  convenience for one thing: asking that the *next* boot be Lab, from your
  phone, without walking over with a dongle. It requests Lab or nothing —
  never Emergency, never over the network — and self-resets once consumed.
  It also implements `publish_result`: after every resolution it writes
  the decision (mode, which selector decided, host, timestamp, and what
  each selector contributed) to `ha_status_entity` — a one-way status
  readout, not a control surface. Not configured or unreachable → no
  preference, and any publish failure is logged and ignored. It speaks to
  a Home Assistant `input_select` + status entity; any other switch (MQTT,
  a REST endpoint, a flag file on an always-on host) is a new module in
  `selectors/`. Nothing about failover depends on it.
- **Logging** is the systemd journal, like any other Proxmox service:
  `journalctl -u coldstandby`. `qmrestore` / `qm` run without output
  capture so their native progress lands in the journal too. A failed run
  exits non-zero, leaving the unit `failed` (visible in `systemctl status`
  and the PVE node summary) and the node powered on.
- **`main` is never contacted.** The set of VMs to keep on standby comes
  only from the `tags` embedded in each backup archive (read with
  `vma config`), never from a live query — a dongle-selected mode is
  exactly when `main` may be gone.

`pve-guests.service` is masked permanently on this node. The controller
re-asserts the mask at the start of **every** run, in every mode (a PVE
upgrade or a stray `systemctl unmask` would otherwise let the next
ordinary boot `startall` a shadow copy of the environment). Restored
guests also get `onboot: 0` forced. All guest starts happen through this
controller's Emergency-mode ordering logic — nothing relies on Proxmox's
native `onboot` / `startall`.

## Orphan cleanup

Each Replication run makes the local standby set exactly match the current
backups: before restoring, it destroys (`qm destroy --purge`) any local VM
that carries the `standby` tag but has no backup on the share. Without this
a VM deleted or untagged on `main` would linger on the backup node forever
and get started in Emergency mode.

Guardrails:
- If the share has **no** standby archives at all (empty, unmounted,
  unreadable), cleanup is skipped entirely — a transient NFS problem must
  never be read as "delete the whole standby set".
- `remove_orphans: false` disables it (add/refresh only, never remove).
- `standby_vmid_range` fences destruction to a VMID range, default
  `1000-1999`; a tagged orphan outside it is logged and left alone. So
  keep your standby guests numbered in that range on `main` (or change the
  fence to match your numbering). Set it to `""` to remove the fence. A
  malformed range fails at config load, not mid-run.
- Removal is by the `standby` tag, so don't hand that tag to a throwaway
  VM you spun up in Lab mode.

## Status

**Implemented:** config loading, mode-selector interface + resolution,
dongle and Home Assistant selectors, NFS mount, backup discovery + tag
reading, restore, orphan cleanup, Emergency ordered start, entry point.

**Not done:** verified against real hardware. `vma config` archive reading,
`qmrestore --force` idempotency across weekly cycles, WOL reachability, and
the wake→restore→shutdown timing budget all need a real run. LXC guests are
out of scope by design (VMs only).

## Layout

```
coldstandby/        the package
  config.py         JSON config + validation
  mode.py           Mode enum, ModeSelector/ModeDecision, determine_mode()
  selectors/        the pluggable mode selectors — add new ones here
    __init__.py     build_selectors() — the priority-ordered list
    dongle.py       DongleSelector — label + marker-token USB stick
    home_assistant.py  HomeAssistantSelector — the online lab switch
  nfs.py            read-only mount context manager
  backups.py        find archives, read embedded tags
  proxmox.py        qmrestore / qm / qm destroy wrappers, startup= parsing
  restore.py        Replication's refresh: orphan cleanup + restore
  replication.py / lab.py / emergency.py   the three mode handlers
  main.py           CLI + dispatch
systemd/coldstandby.service
tests/
```

## Setup

1. `cp config.example.json /etc/coldstandby/config.json` and fill in:
   - `dongle_marker_token` — a random secret (`openssl rand -hex 32`)
   - `restore_storage` — local storage the restored disks land on
   - *(optional)* `ha_base_url` + `ha_token` for the online lab selector;
     leave them empty to skip that layer entirely
2. *(optional, for the online lab selector)* The implementation wired in
   uses Home Assistant: create `input_select.coldstandby_mode` with
   options `replication` and `lab`, default `replication`, and generate a
   long-lived access token for `ha_token`. The boot decision is published
   to `ha_status_entity` (`sensor.coldstandby_boot` by default — HA
   creates it on first write; set `""` to publish nothing). Any other
   switch works via a new module in `coldstandby/selectors/`.
3. Prepare a dongle per mode you want to be able to force: format a small
   USB stick, label its filesystem `COLDSTANDBY-EMERGENCY` (or `-LAB` /
   `-REPLICATION`) with `fatlabel` / `e2label`, and put the same secret as
   `dongle_marker_token` in a file `.coldstandby-token` on it. An Emergency
   dongle is the one that matters; the others are conveniences.
4. `systemctl mask pve-guests.service` (the controller also re-asserts
   this itself on every run, but mask it now so the first boot is clean)
5. Install to `/opt/coldstandby`, copy `systemd/coldstandby.service` to
   `/etc/systemd/system/`, `systemctl daemon-reload && systemctl enable
   coldstandby.service`.
6. Before trusting an unattended boot:
   `python3 -m coldstandby.main --config /etc/coldstandby/config.json --dry-run -v`
   (`--force-mode {replication,lab,emergency}` to exercise one path,
   `--no-shutdown` to keep Replication from powering off).

## Manual replication refresh

`python3 -m coldstandby.main --replicate-now` runs a replication refresh
immediately and stops — no mode resolution, nothing online consulted, and
never a shutdown. Use it to bring the standby disks current by hand while
the node is already up (before Lab work, say). Add `--dry-run` to preview.

## Tests

`pip install -e '.[dev]' && pytest` — stdlib-only runtime, pytest for the
suite. Everything that shells out is mocked; no Proxmox needed.
