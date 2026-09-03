from coldstandby import lab
from coldstandby.config import Config
from coldstandby.proxmox import LocalVM


def _cfg(**kw) -> Config:
    base = dict(ha_base_url="http://x", ha_token="t", dongle_marker_token="s")
    base.update(kw)
    return Config(**base)


def test_lab_never_refreshes(monkeypatch):
    monkeypatch.setattr(lab, "list_local_vms", lambda: [
        LocalVM(100, {"tags": "standby"}),
        LocalVM(200, {"tags": "prod"}),
    ])
    # If lab ever imported/used the refresh path, these would exist to patch;
    # assert instead that the restore module is simply not referenced.
    assert not hasattr(lab, "refresh_standby_vms")
    assert not hasattr(lab, "mounted_backup_share")

    rc = lab.run(_cfg(), dry_run=False)
    assert rc == 0
