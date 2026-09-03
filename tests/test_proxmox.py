import textwrap

import pytest

from coldstandby import proxmox
from coldstandby.config import Config
from coldstandby.proxmox import (
    LocalVM,
    destroy_vm,
    ordered_standby_vms,
    parse_startup,
    parse_vm_conf,
    start_in_order,
)


def _cfg(**kw) -> Config:
    base = dict(ha_base_url="http://x", ha_token="t", dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def test_parse_startup():
    s = parse_startup("order=3,up=45,down=60")
    assert (s.order, s.up_delay, s.down_delay) == (3, 45, 60)

    empty = parse_startup("")
    assert empty.order is None and empty.up_delay is None


def test_startup_sort_key_unset_order_last():
    assert parse_startup("order=1").sort_key < parse_startup("order=2").sort_key
    assert parse_startup("order=99").sort_key < parse_startup("").sort_key


def test_parse_vm_conf_stops_at_snapshot_section():
    text = textwrap.dedent("""\
        boot: order=scsi0
        cores: 2
        tags: standby
        startup: order=2,up=30

        [pre-upgrade]
        cores: 1
        tags: somethingelse
    """)
    conf = parse_vm_conf(text)
    assert conf["cores"] == "2"
    assert conf["tags"] == "standby"
    assert conf["startup"] == "order=2,up=30"


def test_ordered_standby_vms(tmp_path):
    (tmp_path / "100.conf").write_text("tags: standby\nstartup: order=2\n")
    (tmp_path / "101.conf").write_text("tags: standby\nstartup: order=1\n")
    (tmp_path / "102.conf").write_text("tags: standby\n")  # no order -> last
    (tmp_path / "103.conf").write_text("tags: standby\n")  # no order, higher vmid
    (tmp_path / "200.conf").write_text("tags: prod\nstartup: order=1\n")  # not standby

    order = [vm.vmid for vm in ordered_standby_vms(_cfg(), tmp_path)]
    assert order == [101, 100, 102, 103]


def test_start_in_order_delays_between_but_not_after(monkeypatch):
    started, slept = [], []
    monkeypatch.setattr("coldstandby.proxmox.start_vm", lambda vmid, dry_run: started.append(vmid))
    monkeypatch.setattr("coldstandby.proxmox.time.sleep", lambda s: slept.append(s))

    vms = [
        LocalVM(1, {"startup": "order=1,up=10"}),
        LocalVM(2, {"startup": "order=2"}),  # falls back to default
        LocalVM(3, {"startup": "order=3,up=5"}),
    ]
    start_in_order(vms, _cfg(default_up_delay_seconds=30), dry_run=False)

    assert started == [1, 2, 3]
    assert slept == [10, 30]  # nothing after the last


def test_destroy_vm_best_effort_stop_then_purge(monkeypatch):
    runs = []

    class _R:
        returncode = 0

    def fake_run(cmd, timeout=None):
        runs.append(cmd)
        return _R()

    monkeypatch.setattr(proxmox.subprocess, "run", fake_run)
    destroy_vm(101, dry_run=False)

    assert runs[0] == ["qm", "stop", "101"]
    assert runs[1] == ["qm", "destroy", "101", "--purge", "--destroy-unreferenced-disks", "1"]


def test_destroy_vm_ignores_stop_failure_but_not_destroy_failure(monkeypatch):
    def fake_run(cmd, timeout=None):
        rc = 1 if cmd[:2] == ["qm", "stop"] else 0
        return type("R", (), {"returncode": rc})()

    monkeypatch.setattr(proxmox.subprocess, "run", fake_run)
    destroy_vm(101, dry_run=False)  # stop rc=1 tolerated, destroy rc=0 -> no raise

    def fail_destroy(cmd, timeout=None):
        rc = 1 if cmd[:2] == ["qm", "destroy"] else 0
        return type("R", (), {"returncode": rc})()

    monkeypatch.setattr(proxmox.subprocess, "run", fail_destroy)
    with pytest.raises(proxmox.ProxmoxError):
        destroy_vm(101, dry_run=False)


def test_destroy_vm_dry_run_runs_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run must not exec")

    monkeypatch.setattr(proxmox.subprocess, "run", boom)
    destroy_vm(101, dry_run=True)
