import time

from clickwhoosh.bridge import EventDeduper
from clickwhoosh.click_v2 import Button, ButtonEvent, Puck


def _ev(bit: int, is_down: bool) -> ButtonEvent:
    return ButtonEvent(bit=bit, puck=Puck.LEFT, button=Button.SHIFT_UP, is_down=is_down)


def test_duplicate_within_window_is_dropped():
    d = EventDeduper(window_seconds=0.1)
    assert d.is_duplicate(_ev(4, True)) is False
    assert d.is_duplicate(_ev(4, True)) is True


def test_different_direction_is_not_duplicate():
    d = EventDeduper(window_seconds=0.1)
    d.is_duplicate(_ev(4, True))
    assert d.is_duplicate(_ev(4, False)) is False


def test_different_bit_is_not_duplicate():
    d = EventDeduper(window_seconds=0.1)
    d.is_duplicate(_ev(4, True))
    assert d.is_duplicate(_ev(5, True)) is False


def test_outside_window_is_not_duplicate():
    d = EventDeduper(window_seconds=0.01)
    d.is_duplicate(_ev(4, True))
    time.sleep(0.02)
    assert d.is_duplicate(_ev(4, True)) is False
