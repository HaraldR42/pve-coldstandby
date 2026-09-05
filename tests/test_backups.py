import datetime as dt
import subprocess
from pathlib import Path

import pytest

from coldstandby import backups
from coldstandby.backups import (
    BackupArchive,
    _decompressor_argv,
    _latest_per_vmid,
    _parse_archive_name,
    _read_embedded_config,
    config_has_tag,
    newest_archive_age_days,
    parse_vma_config,
)
from coldstandby.config import Config


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


# --- reading the embedded config without fully expanding the archive ----

def _cfg(**kw) -> Config:
    base = dict(dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("vzdump-qemu-100-2026_08_30-02_15_00.vma.zst", ("zstd", "-d", "-c", "-q")),
        ("vzdump-qemu-100-2026_08_30-02_15_00.vma.gz", ("gzip", "-d", "-c")),
        ("vzdump-qemu-100-2026_08_30-02_15_00.vma.lzo", ("lzop", "-d", "-c")),
        ("vzdump-qemu-100-2026_08_30-02_15_00.vma", None),
    ],
)
def test_decompressor_argv_by_suffix(name, expected):
    assert _decompressor_argv(Path(name)) == expected


def test_uncompressed_archive_runs_vma_directly(monkeypatch):
    seen = {}

    def fake_run(argv, *, stdin, capture_output, text, timeout):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return subprocess.CompletedProcess(argv, 0, stdout="tags: standby\n", stderr="")

    monkeypatch.setattr(backups.subprocess, "run", fake_run)
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma"), 100, dt.datetime.now())

    result = _read_embedded_config(archive, _cfg())

    assert result == {"tags": "standby"}
    assert seen["argv"] == ["vma", "config", str(archive.path)]
    assert seen["stdin"] is None


class _FakeStdout:
    """Stands in for the read end of the decompressor's stdout pipe."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePopen:
    """Stands in for the decompressor subprocess.Popen."""

    instances = []

    def __init__(self, argv, stdout=None, stderr=None):
        self.argv = argv
        self.stdout = _FakeStdout()
        self.waited_timeout = None
        self.killed = False
        _FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.waited_timeout = timeout

    def kill(self):
        self.killed = True


def test_compressed_archive_pipes_decompressor_into_vma(monkeypatch):
    _FakePopen.instances.clear()
    monkeypatch.setattr(backups.subprocess, "Popen", _FakePopen)

    seen = {}

    def fake_run(argv, *, stdin, capture_output, text, timeout):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return subprocess.CompletedProcess(argv, 0, stdout="tags: standby\n", stderr="")

    monkeypatch.setattr(backups.subprocess, "run", fake_run)
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma.zst"), 100, dt.datetime.now())

    result = _read_embedded_config(archive, _cfg())

    assert result == {"tags": "standby"}
    assert seen["argv"] == ["vma", "config", "-"]
    popen = _FakePopen.instances[0]
    assert popen.argv == ["zstd", "-d", "-c", "-q", str(archive.path)]
    assert seen["stdin"] is popen.stdout
    # the pipe was closed and the decompressor reaped -- not left running
    # against the rest of the archive, and not leaked as a zombie.
    assert popen.stdout.closed is True
    assert popen.waited_timeout == backups._DECOMPRESSOR_REAP_TIMEOUT


def test_decompressor_missing_binary_is_handled(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("zstd")

    monkeypatch.setattr(backups.subprocess, "Popen", boom)
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma.zst"), 100, dt.datetime.now())
    assert _read_embedded_config(archive, _cfg()) is None


def test_decompressor_is_reaped_even_if_vma_fails(monkeypatch):
    _FakePopen.instances.clear()
    monkeypatch.setattr(backups.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        backups.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="not a vma file"),
    )
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma.zst"), 100, dt.datetime.now())

    assert _read_embedded_config(archive, _cfg()) is None
    assert _FakePopen.instances[0].waited_timeout == backups._DECOMPRESSOR_REAP_TIMEOUT


def test_stuck_decompressor_is_killed(monkeypatch):
    class _HangingPopen(_FakePopen):
        def wait(self, timeout=None):
            self.waited_timeout = timeout
            if not self.killed:
                raise subprocess.TimeoutExpired(cmd="zstd", timeout=timeout)

    _FakePopen.instances.clear()
    monkeypatch.setattr(backups.subprocess, "Popen", _HangingPopen)
    monkeypatch.setattr(
        backups.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="tags: standby\n", stderr=""),
    )
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma.zst"), 100, dt.datetime.now())

    assert _read_embedded_config(archive, _cfg()) == {"tags": "standby"}
    assert _FakePopen.instances[0].killed is True


def test_vma_timeout_is_handled(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="vma", timeout=1)

    monkeypatch.setattr(backups.subprocess, "run", timeout)
    archive = BackupArchive(Path("/nfs/vzdump-qemu-100-2026_08_30-02_15_00.vma"), 100, dt.datetime.now())
    assert _read_embedded_config(archive, _cfg()) is None
