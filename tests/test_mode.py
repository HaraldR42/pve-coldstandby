import pytest

from coldstandby.mode import (
    REQUEST_NONE,
    REQUEST_NOT_CONSULTED,
    REQUEST_UNAVAILABLE,
    Mode,
    ModeDecision,
    ModeSelector,
    ModeSelectorUnavailable,
    determine_mode,
)


class FakeSelector(ModeSelector):
    def __init__(self, requests=None, unavailable=False, clear_fails=False, publish_fails=False):
        self._requests = requests
        self._unavailable = unavailable
        self._clear_fails = clear_fails
        self._publish_fails = publish_fails
        self.cleared = 0
        self.published: ModeDecision | None = None

    def mode_requested(self):
        if self._unavailable:
            raise ModeSelectorUnavailable("down")
        return self._requests

    def clear(self):
        if self._clear_fails:
            raise ModeSelectorUnavailable("write failed")
        self.cleared += 1

    def publish_result(self, decision):
        if self._publish_fails:
            raise RuntimeError("sink down")
        self.published = decision


def test_no_selectors_is_replication():
    assert determine_mode([]) is Mode.REPLICATION


def test_no_selector_has_an_opinion_is_replication():
    a, b = FakeSelector(None), FakeSelector(None)
    assert determine_mode([a, b]) is Mode.REPLICATION
    assert (a.cleared, b.cleared) == (0, 0)


@pytest.mark.parametrize("mode", list(Mode))
def test_first_opinion_wins_and_is_consumed(mode):
    winner = FakeSelector(mode)
    loser = FakeSelector(Mode.EMERGENCY)
    assert determine_mode([winner, loser]) is mode
    assert winner.cleared == 1
    assert loser.cleared == 0  # never reached


def test_priority_is_list_order():
    first = FakeSelector(Mode.LAB)
    second = FakeSelector(Mode.EMERGENCY)
    assert determine_mode([first, second]) is Mode.LAB


def test_selector_without_opinion_falls_through():
    quiet = FakeSelector(None)
    speaking = FakeSelector(Mode.LAB)
    assert determine_mode([quiet, speaking]) is Mode.LAB
    assert speaking.cleared == 1


def test_unavailable_selector_is_skipped():
    broken = FakeSelector(unavailable=True)
    speaking = FakeSelector(Mode.EMERGENCY)
    assert determine_mode([broken, speaking]) is Mode.EMERGENCY


def test_all_unavailable_is_replication():
    assert determine_mode([FakeSelector(unavailable=True)]) is Mode.REPLICATION


def test_win_stands_even_if_clear_fails():
    sel = FakeSelector(Mode.LAB, clear_fails=True)
    assert determine_mode([sel]) is Mode.LAB


# --- publish_result -------------------------------------------------

def test_every_selector_gets_the_decision():
    class A(FakeSelector): ...
    class B(FakeSelector): ...
    class C(FakeSelector): ...

    a = A(unavailable=True)     # skipped during resolution
    b = B(Mode.LAB)             # decides
    c = C(Mode.EMERGENCY)       # never consulted (lower priority)
    determine_mode([a, b, c])

    for sel in (a, b, c):
        assert isinstance(sel.published, ModeDecision)
        assert sel.published.mode is Mode.LAB
        assert sel.published.decided_by == "B"

    assert b.published.selector_requests == {
        "A": REQUEST_UNAVAILABLE,
        "B": "lab",
        "C": REQUEST_NOT_CONSULTED,
    }


def test_fallback_decision_has_no_decider():
    sel = FakeSelector(None)
    determine_mode([sel])
    assert sel.published.mode is Mode.REPLICATION
    assert sel.published.decided_by is None


def test_publish_can_be_suppressed():
    sel = FakeSelector(Mode.LAB)
    assert determine_mode([sel], publish=False) is Mode.LAB
    assert sel.published is None


def test_publish_failure_is_swallowed():
    boom = FakeSelector(publish_fails=True)
    ok = FakeSelector(Mode.LAB)
    # must not raise, and the other selector still gets published to
    assert determine_mode([boom, ok]) is Mode.LAB
    assert ok.published is not None


def test_decision_serialisation():
    d = ModeDecision(mode=Mode.LAB, decided_by="X", selector_requests={"X": "lab"})
    assert d.summary() == "lab (X)"
    blob = d.as_dict()
    assert blob["mode"] == "lab" and blob["decided_by"] == "X"
    assert blob["selectors"] == {"X": "lab"}
    assert "T" in blob["resolved_at"]  # isoformat
