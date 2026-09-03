import contextlib
from pathlib import Path

import pytest

from coldstandby import replication, restore
from coldstandby.backups import BackupArchive, StandbyBackup
from coldstandby.config import Config
from coldstandby.proxmox import LocalVM, ProxmoxError
import datetime as dt


def _cfg(**kw) -> Config:
    base = dict(ha_base_url="http://x", ha_token="t", dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def _backup(vmid, days_old):
    ts = dt.datetime.now() - dt.timedelta(days=days_old)
    return StandbyBackup(BackupArchive(Path(f"/nfs/vzdump-qemu-{vmid}.vma.zst"), vmid, ts), {})


@pytest.fixture
def patched(monkeypatch):
    @contextlib.contextmanager
    def fake_mount(cfg, dry_run=False):
        yield Path("/nfs")

    monkeypatch.setattr(restore, "mounted_backup_share", fake_mount)
    monkeypatch.setattr(restore, "list_local_vms", lambda: [])
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: None)
    calls = []
    monkeypatch.setattr(restore, "restore_vm", lambda a, v, c, dry_run: calls.append(v))
    return calls


def test_refresh_restores_all(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 1), _backup(101, 2)])
    result = restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert result.restored == [100, 101]
    assert result.failed == []
    assert result.ok


def test_refresh_continues_past_one_failure(monkeypatch):
    @contextlib.contextmanager
    def fake_mount(cfg, dry_run=False):
        yield Path("/nfs")

    monkeypatch.setattr(restore, "mounted_backup_share", fake_mount)
    monkeypatch.setattr(restore, "list_local_vms", lambda: [])
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 1), _backup(101, 1)])

    def flaky(archive, vmid, cfg, dry_run):
        if vmid == 100:
            raise ProxmoxError("boom")

    monkeypatch.setattr(restore, "restore_vm", flaky)
    result = restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert result.restored == [101]
    assert result.failed == [100]
    assert not result.ok


def test_refresh_empty(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [])
    result = restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert result.restored == [] and not result.ok


def _local(vmid, tags="standby"):
    return LocalVM(vmid, {"tags": tags})


def test_orphan_tagged_vm_is_removed(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(1100, 1)])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(1100), _local(1101), _local(1102, "prod")])
    destroyed = []
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: destroyed.append(vmid))

    result = restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert destroyed == [1101]         # 1100 is still backed up, 1102 isn't tagged
    assert result.removed == [1101]
    assert result.restored == [1100]
    assert result.ok


def test_orphan_cleanup_disabled(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(1100, 1)])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(1100), _local(1101)])
    destroyed = []
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: destroyed.append(vmid))

    result = restore.refresh_standby_vms(_cfg(remove_orphans=False), dry_run=False)
    assert destroyed == []
    assert result.removed == []


def test_orphan_outside_vmid_fence_is_left_alone(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(1101, 1)])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(1102), _local(42)])
    destroyed = []
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: destroyed.append(vmid))

    result = restore.refresh_standby_vms(_cfg(), dry_run=False)  # default fence 1000-1999
    assert destroyed == [1102]   # 42 is a tagged orphan but outside the fence
    assert result.removed == [1102]


def test_orphan_fence_can_be_disabled(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(1101, 1)])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(42)])
    destroyed = []
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: destroyed.append(vmid))

    restore.refresh_standby_vms(_cfg(standby_vmid_range=""), dry_run=False)
    assert destroyed == [42]


def test_orphan_removal_failure_marks_run_not_ok(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(1100, 1)])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(1101)])

    def boom(vmid, dry_run):
        raise ProxmoxError("destroy failed")

    monkeypatch.setattr(restore, "destroy_vm", boom)
    result = restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert result.failed == [1101]
    assert not result.ok


def test_empty_share_skips_orphan_cleanup(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [])
    monkeypatch.setattr(restore, "list_local_vms", lambda: [_local(1101)])
    destroyed = []
    monkeypatch.setattr(restore, "destroy_vm", lambda vmid, dry_run: destroyed.append(vmid))

    restore.refresh_standby_vms(_cfg(), dry_run=False)
    assert destroyed == []   # never delete the standby set when the share looks empty


def test_replication_shuts_down_on_success(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 1)])
    poweroffs = []
    monkeypatch.setattr(replication.subprocess, "run", lambda cmd, check=False: poweroffs.append(cmd))
    rc = replication.run(_cfg(), dry_run=False, allow_shutdown=True)
    assert rc == 0
    assert poweroffs == [["systemctl", "poweroff"]]


def test_replication_stays_up_on_failure(monkeypatch):
    @contextlib.contextmanager
    def fake_mount(cfg, dry_run=False):
        yield Path("/nfs")

    monkeypatch.setattr(restore, "mounted_backup_share", fake_mount)
    monkeypatch.setattr(restore, "list_local_vms", lambda: [])
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 1)])
    monkeypatch.setattr(restore, "restore_vm", lambda a, v, c, dry_run: (_ for _ in ()).throw(ProxmoxError("x")))
    poweroffs = []
    monkeypatch.setattr(replication.subprocess, "run", lambda cmd, check=False: poweroffs.append(cmd))
    rc = replication.run(_cfg(), dry_run=False, allow_shutdown=True)
    assert rc == 1
    assert poweroffs == []


def test_replication_stale_warning(monkeypatch, patched, caplog):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 20)])
    monkeypatch.setattr(replication.subprocess, "run", lambda cmd, check=False: None)
    with caplog.at_level("WARNING"):
        replication.run(_cfg(staleness_warn_days=8.0), dry_run=False, allow_shutdown=True)
    assert any("days old" in r.message for r in caplog.records)


def test_replication_no_shutdown_flag(monkeypatch, patched):
    monkeypatch.setattr(restore, "select_standby_backups", lambda s, c: [_backup(100, 1)])
    poweroffs = []
    monkeypatch.setattr(replication.subprocess, "run", lambda cmd, check=False: poweroffs.append(cmd))
    replication.run(_cfg(), dry_run=False, allow_shutdown=False)
    assert poweroffs == []
