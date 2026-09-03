import types

from coldstandby.selectors import dongle
from coldstandby.config import Config
from coldstandby.selectors.dongle import DongleSelector
from coldstandby.mode import Mode, ModeSelector


def _cfg(**kw) -> Config:
    base = dict(dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def _sel(**kw) -> DongleSelector:
    return DongleSelector(_cfg(**kw))


def _find_only(suffix):
    """DongleSelector._find_device_by_label stub: a device only for the
    label ending <suffix>."""
    return staticmethod(lambda label: "/dev/sdz1" if label.endswith(suffix) else None)


def _token(ok: bool):
    return lambda self, device: ok


def test_is_a_mode_selector():
    assert isinstance(_sel(), ModeSelector)


def test_no_dongle_is_no_opinion(monkeypatch):
    monkeypatch.setattr(DongleSelector, "_find_device_by_label", staticmethod(lambda label: None))
    assert _sel().mode_requested() is None


def test_blkid_missing_is_no_opinion(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("blkid")

    monkeypatch.setattr(dongle.subprocess, "run", boom)
    assert DongleSelector._find_device_by_label("COLDSTANDBY-LAB") is None
    assert _sel().mode_requested() is None


def test_valid_lab_dongle(monkeypatch):
    monkeypatch.setattr(DongleSelector, "_find_device_by_label", _find_only("LAB"))
    monkeypatch.setattr(DongleSelector, "_marker_token_matches", _token(True))
    assert _sel().mode_requested() is Mode.LAB


def test_valid_emergency_dongle(monkeypatch):
    monkeypatch.setattr(DongleSelector, "_find_device_by_label", _find_only("EMERGENCY"))
    monkeypatch.setattr(DongleSelector, "_marker_token_matches", _token(True))
    assert _sel().mode_requested() is Mode.EMERGENCY


def test_bad_token_is_ignored(monkeypatch):
    monkeypatch.setattr(DongleSelector, "_find_device_by_label", _find_only("EMERGENCY"))
    monkeypatch.setattr(DongleSelector, "_marker_token_matches", _token(False))
    assert _sel().mode_requested() is None


def test_multiple_mode_dongles_refused(monkeypatch):
    monkeypatch.setattr(DongleSelector, "_find_device_by_label", staticmethod(lambda label: "/dev/sdz1"))
    monkeypatch.setattr(DongleSelector, "_marker_token_matches", _token(True))
    assert _sel().mode_requested() is None


def test_label_prefix_and_suffixes(monkeypatch):
    seen = []
    monkeypatch.setattr(
        DongleSelector, "_find_device_by_label",
        staticmethod(lambda label: seen.append(label) or None),
    )
    _sel(dongle_label_prefix="FOO").mode_requested()
    assert seen == ["FOO-EMERGENCY", "FOO-LAB", "FOO-REPLICATION"]


def test_clear_is_a_noop():
    _sel().clear()  # stateless -- must not raise


def _fake_mount_ok():
    return lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_marker_token_matches(monkeypatch, tmp_path):
    (tmp_path / ".coldstandby-token").write_text("sekret\n")
    monkeypatch.setattr(dongle.subprocess, "run", _fake_mount_ok())
    sel = _sel(dongle_marker_token="sekret", dongle_mount_point=tmp_path)
    assert sel._marker_token_matches("/dev/sdz1") is True


def test_marker_token_mismatch(monkeypatch, tmp_path):
    (tmp_path / ".coldstandby-token").write_text("wrong")
    monkeypatch.setattr(dongle.subprocess, "run", _fake_mount_ok())
    sel = _sel(dongle_marker_token="sekret", dongle_mount_point=tmp_path)
    assert sel._marker_token_matches("/dev/sdz1") is False


def test_marker_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dongle.subprocess, "run", _fake_mount_ok())
    sel = _sel(dongle_marker_token="sekret", dongle_mount_point=tmp_path)
    assert sel._marker_token_matches("/dev/sdz1") is False


def test_marker_mount_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dongle.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="no"),
    )
    sel = _sel(dongle_marker_token="sekret", dongle_mount_point=tmp_path)
    assert sel._marker_token_matches("/dev/sdz1") is False
