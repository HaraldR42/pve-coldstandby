import datetime as dt

import pytest

from coldstandby.backups import (
    _latest_per_vmid,
    _parse_archive_name,
    config_has_tag,
    newest_archive_age_days,
    parse_vma_config,
)
from coldstandby.backups import BackupArchive


def _arc(name: str):
    from pathlib import Path

    return _parse_archive_name(Path(name))


def test_parse_archive_name_variants():
    a = _arc("vzdump-qemu-100-2026_08_30-02_15_00.vma.zst")
    assert a is not None
    assert a.vmid == 100
    assert a.taken_at == dt.datetime(2026, 8, 30, 2, 15, 0)

    assert _arc("vzdump-qemu-101-2026_08_30-02_15_00.vma") is not None
    assert _arc("vzdump-qemu-101-2026_08_30-02_15_00.vma.gz") is not None


def test_parse_archive_name_rejects_non_matching():
    assert _arc("vzdump-lxc-100-2026_08_30-02_15_00.tar.zst") is None
    assert _arc("vzdump-qemu-100-2026_08_30-02_15_00.vma.zst.notes") is None
    assert _arc("random.txt") is None


def test_latest_per_vmid_picks_newest():
    from pathlib import Path

    old = BackupArchive(Path("a"), 100, dt.datetime(2026, 8, 1))
    new = BackupArchive(Path("b"), 100, dt.datetime(2026, 8, 30))
    other = BackupArchive(Path("c"), 200, dt.datetime(2026, 8, 15))
    latest = _latest_per_vmid([old, new, other])
    assert latest[100] is new
    assert latest[200] is other


def test_parse_vma_config_ignores_comments():
    text = "\n".join([
        "#qmdump#map:scsi0:drive-scsi0:local-lvm:raw:",
        "boot: order=scsi0",
        "cores: 4",
        "tags: standby;prod",
        "scsi0: local-lvm:vm-100-disk-0,size=32G",
    ])
    cfg = parse_vma_config(text)
    assert cfg["cores"] == "4"
    assert cfg["tags"] == "standby;prod"
    assert "#qmdump#map" not in cfg


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("standby", True),
        ("standby;prod", True),
        ("prod,standby", True),
        ("prod backup", False),
        ("", False),
        ("standbyish", False),
    ],
)
def test_config_has_tag(raw, expected):
    assert config_has_tag({"tags": raw}, "standby") is expected


def test_newest_archive_age_days_uses_oldest_selected():
    from pathlib import Path
    from coldstandby.backups import StandbyBackup

    now = dt.datetime.now()
    b1 = StandbyBackup(BackupArchive(Path("a"), 1, now - dt.timedelta(days=2)), {})
    b2 = StandbyBackup(BackupArchive(Path("b"), 2, now - dt.timedelta(days=9)), {})
    age = newest_archive_age_days([b1, b2])
    assert 8.9 < age < 9.1
    assert newest_archive_age_days([]) is None
