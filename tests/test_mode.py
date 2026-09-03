import pytest

from coldstandby.mode import Mode, ModeSelector, ModeSelectorUnavailable, determine_mode


class FakeSelector(ModeSelector):
    def __init__(self, requests=None, unavailable=False, clear_fails=False):
        self._requests = requests
        self._unavailable = unavailable
        self._clear_fails = clear_fails
        self.cleared = 0

    def mode_requested(self):
        if self._unavailable:
            raise ModeSelectorUnavailable("down")
        return self._requests

    def clear(self):
        if self._clear_fails:
            raise ModeSelectorUnavailable("write failed")
        self.cleared += 1


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
