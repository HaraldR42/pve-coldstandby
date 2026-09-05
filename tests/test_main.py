import subprocess
import types

from coldstandby import main as main_mod
from coldstandby.main import PVE_GUESTS_UNIT, _enforce_pve_guests_masked


def _fake_run(results):
    """results: dict mapping the systemctl subcommand to a CompletedProcess."""
    calls = []

    def run(cmd, capture_output=False, text=False, check=False, timeout=None):
        calls.append(cmd)
        outcome = results[cmd[1]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return run, calls


def _cp(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_already_masked_is_noop(monkeypatch):
    run, calls = _fake_run({"is-enabled": _cp(stdout="masked\n", returncode=1)})
    monkeypatch.setattr(subprocess, "run", run)
    _enforce_pve_guests_masked(dry_run=False)
    assert [c[1] for c in calls] == ["is-enabled"]  # never called mask


def test_masks_when_not_masked(monkeypatch):
    run, calls = _fake_run({
        "is-enabled": _cp(stdout="enabled\n"),
        "mask": _cp(returncode=0),
    })
    monkeypatch.setattr(subprocess, "run", run)
    _enforce_pve_guests_masked(dry_run=False)
    assert ["systemctl", "mask", PVE_GUESTS_UNIT] in calls


def test_dry_run_never_masks(monkeypatch):
    run, calls = _fake_run({"is-enabled": _cp(stdout="enabled\n")})
    monkeypatch.setattr(subprocess, "run", run)
    _enforce_pve_guests_masked(dry_run=True)
    assert [c[1] for c in calls] == ["is-enabled"]


def test_missing_systemctl_does_not_raise(monkeypatch):
    run, _ = _fake_run({"is-enabled": FileNotFoundError("systemctl")})
    monkeypatch.setattr(subprocess, "run", run)
    _enforce_pve_guests_masked(dry_run=False)  # must not raise


def test_main_enforces_before_dispatch(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"ha_base_url":"http://x","ha_token":"t","dongle_marker_token":"s"}')

    order = []
    monkeypatch.setattr(main_mod, "_enforce_pve_guests_masked", lambda dry_run: order.append("mask"))
    monkeypatch.setattr(main_mod.emergency, "run", lambda cfg, dry_run: order.append("dispatch") or 0)

    rc = main_mod.main(["--config", str(cfg), "--force-mode", "emergency", "--dry-run"])
    assert rc == 0
    assert order == ["mask", "dispatch"]


def _cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"ha_base_url":"http://x","ha_token":"t","dongle_marker_token":"s"}')
    return p


def test_replicate_now_runs_replication_without_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "_enforce_pve_guests_masked", lambda dry_run: None)

    seen = {}

    def fake_replication_run(cfg, *, dry_run, allow_shutdown):
        seen["dry_run"] = dry_run
        seen["allow_shutdown"] = allow_shutdown
        return 0

    monkeypatch.setattr(main_mod.replication, "run", fake_replication_run)

    def no_resolve(*a, **k):
        raise AssertionError("mode resolution must not run for --replicate-now")

    monkeypatch.setattr(main_mod, "determine_mode", no_resolve)

    rc = main_mod.main(["--config", str(_cfg_file(tmp_path)), "--replicate-now"])
    assert rc == 0
    assert seen == {"dry_run": False, "allow_shutdown": False}


def test_replicate_now_conflicts_with_force_mode(tmp_path, capsys):
    import pytest

    with pytest.raises(SystemExit):
        main_mod.main([
            "--config", str(_cfg_file(tmp_path)),
            "--replicate-now", "--force-mode", "replication",
        ])


def _patched_main(monkeypatch, tmp_path):
    """Wire up main() with everything stubbed; return the seen-args dict."""
    monkeypatch.setattr(main_mod, "_enforce_pve_guests_masked", lambda dry_run: None)
    monkeypatch.setattr(main_mod, "build_selectors", lambda cfg: [])
    seen = {}

    def fake_determine(selectors, *, dry_run):
        seen["resolve_dry_run"] = dry_run
        return main_mod.Mode.LAB

    def fake_lab_run(cfg, *, dry_run):
        seen["handler_dry_run"] = dry_run
        return 0

    monkeypatch.setattr(main_mod, "determine_mode", fake_determine)
    monkeypatch.setattr(main_mod.lab, "run", fake_lab_run)
    return seen


def test_plain_dry_run_is_dry_for_selectors_and_handler(monkeypatch, tmp_path):
    seen = _patched_main(monkeypatch, tmp_path)
    assert main_mod.main(["--config", str(_cfg_file(tmp_path)), "--dry-run"]) == 0
    assert seen == {"resolve_dry_run": True, "handler_dry_run": True}


def test_exercise_selectors_runs_selectors_live_but_handler_dry(monkeypatch, tmp_path):
    seen = _patched_main(monkeypatch, tmp_path)
    rc = main_mod.main([
        "--config", str(_cfg_file(tmp_path)), "--dry-run", "--exercise-selectors",
    ])
    assert rc == 0
    assert seen == {"resolve_dry_run": False, "handler_dry_run": True}


def test_normal_run_is_live_for_both(monkeypatch, tmp_path):
    seen = _patched_main(monkeypatch, tmp_path)
    assert main_mod.main(["--config", str(_cfg_file(tmp_path))]) == 0
    assert seen == {"resolve_dry_run": False, "handler_dry_run": False}


def test_exercise_selectors_without_dry_run_errors(tmp_path):
    import pytest

    with pytest.raises(SystemExit):
        main_mod.main(["--config", str(_cfg_file(tmp_path)), "--exercise-selectors"])
