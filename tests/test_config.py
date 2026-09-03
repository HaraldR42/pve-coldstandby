import json

import pytest

from coldstandby.config import Config


def _write(tmp_path, data: dict):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return p


def test_load_minimal(tmp_path):
    p = _write(tmp_path, {"dongle_marker_token": "secret"})
    cfg = Config.load(p)
    assert cfg.standby_tag == "standby"
    assert cfg.shutdown_after_replication is True
    assert cfg.online_selector_enabled is False  # no ha_* -> disabled


def test_online_selector_enabled_when_both_set(tmp_path):
    p = _write(tmp_path, {
        "dongle_marker_token": "s",
        "ha_base_url": "http://ha:8123",
        "ha_token": "tok",
    })
    assert Config.load(p).online_selector_enabled is True


@pytest.mark.parametrize("half", [
    {"ha_base_url": "http://ha"},
    {"ha_token": "tok"},
])
def test_half_configured_online_selector_exits(tmp_path, half):
    p = _write(tmp_path, {"dongle_marker_token": "s", **half})
    with pytest.raises(SystemExit):
        Config.load(p)


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        Config.load(tmp_path / "nope.json")


def test_empty_dongle_token_exits(tmp_path):
    p = _write(tmp_path, {"ha_base_url": "http://ha", "ha_token": "t"})
    with pytest.raises(SystemExit):
        Config.load(p)


def test_unknown_key_exits(tmp_path):
    p = _write(tmp_path, {
        "ha_base_url": "http://ha",
        "ha_token": "t",
        "dongle_marker_token": "s",
        "wat": 1,
    })
    with pytest.raises(SystemExit):
        Config.load(p)


def test_path_fields_coerced(tmp_path):
    p = _write(tmp_path, {
        "ha_base_url": "http://ha",
        "ha_token": "t",
        "dongle_marker_token": "s",
        "nfs_mount_point": "/mnt/x",
    })
    cfg = Config.load(p)
    assert cfg.nfs_mount_point.name == "x"


def _minimal(tmp_path, **extra):
    return _write(tmp_path, {
        "ha_base_url": "http://ha", "ha_token": "t", "dongle_marker_token": "s",
        **extra,
    })


def test_vmid_range_default(tmp_path):
    assert Config.load(_minimal(tmp_path)).orphan_vmid_bounds() == (1000, 1999)


def test_vmid_range_disabled_with_empty_string(tmp_path):
    cfg = Config.load(_minimal(tmp_path, standby_vmid_range=""))
    assert cfg.orphan_vmid_bounds() is None


def test_vmid_range_parsed(tmp_path):
    cfg = Config.load(_minimal(tmp_path, standby_vmid_range="9000-9999"))
    assert cfg.orphan_vmid_bounds() == (9000, 9999)


@pytest.mark.parametrize("bad", ["9000", "abc-def", "9999-9000", "9000-"])
def test_vmid_range_malformed_exits_at_load(tmp_path, bad):
    with pytest.raises(SystemExit):
        Config.load(_minimal(tmp_path, standby_vmid_range=bad))
