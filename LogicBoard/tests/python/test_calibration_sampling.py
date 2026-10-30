"""
test_calibration_sampling.py — one bad tap costs one tap, not the whole run.

Runs under pytest, and also standalone
(``python tests/python/test_calibration_sampling.py``).

The thing being protected
-------------------------
Calibration collects 5 taps (10 for the palm) and every one of them has to
arrive as the same KIND of input — a tip the board reports as a pen event
on some taps and as touch on others has no single profile to describe it.

That rule is right. Where it was ENFORCED was not: at the end, over the
finished set. One odd tap out of ten and the whole run was thrown away with
nothing to do but start the ten again. On a board that mis-reports
occasionally, the odds of ten clean in a row are not good, and the flow
could be unwinnable in practice — you would never find out until the tenth
tap, every time.

Now the check runs per sample, as each one arrives. A tap that does not
match the run is dropped on its own: the counter does not move, the dot
stays empty, and the user is asked for that one tap again.

Everything here drives ``CalibrationDialog._record_sample`` — the real
funnel every touch, tablet and mouse sample goes through — rather than
poking at internals, so it fails if the behaviour regresses whatever the
implementation looks like.
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.panels.calibration_dialog import (
    CalibrationDialog, config_for, SAMPLE_COOLDOWN_S,
)
from ui.util.input_calibration import (
    TouchSample, SOURCE_TOUCH, SOURCE_TABLET, SOURCE_MOUSE,
)


# ── scaffolding ─────────────────────────────────────────────────────────────

def _dlg(kind="stylus"):
    d = CalibrationDialog(config_for(kind), allow_mouse=True)
    return d


def _touch(source=SOURCE_TOUCH, area=140.0):
    """A sample shaped like the ones _CaptureArea builds."""
    w = h = (area / 3.14159) ** 0.5 * 2.0
    return TouchSample(area=area, width=w, height=h, touch_points=1,
                       source=source)


def _tap(dlg, sample):
    """Feed one sample through the real funnel, clear of the debounce.

    The cooldown is wall-clock, and the point of these tests is the SOURCE
    gate rather than the timing one, so step past it deliberately instead of
    sleeping — sleeping 250 ms per tap would make the palm cases take three
    seconds for no benefit.
    """
    dlg._last_sample_at -= SAMPLE_COOLDOWN_S * 2
    dlg._record_sample(sample)
    _app.processEvents()


def _accepted(dlg):
    return len(dlg._samples)


# ── the core change ─────────────────────────────────────────────────────────

def test_a_mismatched_tap_costs_one_tap_and_not_the_run():
    """THE request. Four good taps, one stray, and the four are still there."""
    d = _dlg()
    for _ in range(4):
        _tap(d, _touch(SOURCE_TOUCH))
    assert _accepted(d) == 4

    _tap(d, _touch(SOURCE_TABLET))
    assert _accepted(d) == 4, (
        "the stray tap was counted, or it wiped the run — either way the "
        "user does not get their four taps back")
    assert not d.completed, "a dropped sample must not finish the run"


def test_the_dropped_tap_can_simply_be_taken_again():
    """And the run finishes normally afterwards — no Retry, no restart."""
    d = _dlg()
    for _ in range(4):
        _tap(d, _touch(SOURCE_TOUCH))
    _tap(d, _touch(SOURCE_MOUSE))      # stray
    _tap(d, _touch(SOURCE_TOUCH))      # the same tap, taken again
    assert _accepted(d) == 5
    assert d.completed, "the run did not complete after replacing the tap"
    assert d.result_profile is not None


def test_a_stray_tap_does_not_fill_a_progress_dot():
    d = _dlg()
    _tap(d, _touch(SOURCE_TOUCH))
    before = d._progress.text()
    _tap(d, _touch(SOURCE_TABLET))
    assert d._progress.text() == before, (
        f"progress moved on a rejected tap: {before!r} -> "
        f"{d._progress.text()!r}")


def test_the_user_is_told_which_tap_was_dropped_and_why():
    d = _dlg()
    _tap(d, _touch(SOURCE_TOUCH))
    _tap(d, _touch(SOURCE_TABLET))
    msg = d._instruction.text()
    assert "wasn't counted" in msg, msg
    # Both families named: "it was wrong" is not actionable, "it came in as
    # pen when the rest were touch" is.
    assert "pen / tablet" in msg and "touch" in msg, msg


def test_the_notice_clears_once_a_good_tap_lands():
    """Otherwise the screen keeps saying a tap was dropped after one wasn't."""
    d = _dlg()
    _tap(d, _touch(SOURCE_TOUCH))
    _tap(d, _touch(SOURCE_TABLET))
    assert d._capture_hint.text() == "Tap again"
    _tap(d, _touch(SOURCE_TOUCH))
    assert d._capture_hint.text() == "Tap here"
    assert d._instruction.text() == d._config.instruction


# ── which family the run anchors to ─────────────────────────────────────────

def test_the_first_tap_decides_what_the_rest_must_be():
    """Whichever family arrives first is the one the run is measuring."""
    d = _dlg()
    _tap(d, _touch(SOURCE_TABLET))
    assert _accepted(d) == 1, "the very first tap must always be accepted"
    _tap(d, _touch(SOURCE_TOUCH))
    assert _accepted(d) == 1, "a touch tap slipped into a tablet-anchored run"
    _tap(d, _touch(SOURCE_TABLET))
    assert _accepted(d) == 2


def test_retry_re_anchors_so_a_bad_first_tap_is_recoverable():
    """The one case per-sample rejection cannot fix by itself: if the FIRST
    tap was the odd one, every good tap after it is the mismatch. Retry has
    to clear the anchor or the user is stuck in a loop."""
    d = _dlg()
    _tap(d, _touch(SOURCE_MOUSE))       # anchored to the wrong thing
    _tap(d, _touch(SOURCE_TOUCH))
    assert _accepted(d) == 1

    d._reset()
    assert d._expected_source is None, "Retry did not clear the anchor"
    for _ in range(5):
        _tap(d, _touch(SOURCE_TOUCH))
    assert _accepted(d) == 5 and d.completed


def test_a_run_of_one_family_throughout_is_untouched():
    """The common path has to stay exactly as fast as it was."""
    for src in (SOURCE_TOUCH, SOURCE_TABLET, SOURCE_MOUSE):
        d = _dlg()
        for _ in range(5):
            _tap(d, _touch(src))
        assert _accepted(d) == 5, src
        assert d.completed, src


def test_the_palm_flow_gets_the_same_treatment():
    """Ten taps is where losing the run hurts most, so check it explicitly."""
    d = _dlg("palm")
    for _ in range(9):
        _tap(d, _touch(SOURCE_TOUCH, area=2400.0))
    _tap(d, _touch(SOURCE_TABLET, area=2400.0))
    assert _accepted(d) == 9, "nine palm rests were thrown away"
    _tap(d, _touch(SOURCE_TOUCH, area=2400.0))
    assert _accepted(d) == 10 and d.completed


# ── the debounce still works, and still works WITH the new gate ─────────────

def test_the_debounce_is_unchanged_for_matching_taps():
    d = _dlg()
    d._record_sample(_touch(SOURCE_TOUCH))
    d._record_sample(_touch(SOURCE_TOUCH))     # immediately after
    assert _accepted(d) == 1, "the cooldown stopped swallowing repeat frames"


def test_one_tap_delivered_as_two_families_still_counts_once():
    """A board that reports a single tap through two sensors at once used to
    fill two dots — and now, worse, could have had the second delivery
    rejected AND cost the user the tap. Neither: one physical tap, one
    sample."""
    d = _dlg()
    d._last_sample_at = 0.0
    d._record_sample(_touch(SOURCE_TOUCH))     # anchors
    d._record_sample(_touch(SOURCE_TABLET))    # same tap, other sensor
    assert _accepted(d) == 1


def test_the_mismatched_half_of_a_double_delivery_does_not_eat_the_good_half():
    """The ordering that makes this work: a rejected sample must not start a
    fresh cooldown. If it did, a board that delivers the WRONG family first
    would swallow the right one milliseconds later, and the user would tap
    forever."""
    d = _dlg()
    _tap(d, _touch(SOURCE_TOUCH))              # anchors to touch
    # A second physical tap arriving as tablet-then-touch, microseconds apart.
    d._last_sample_at -= SAMPLE_COOLDOWN_S * 2
    d._record_sample(_touch(SOURCE_TABLET))    # rejected
    d._record_sample(_touch(SOURCE_TOUCH))     # must still land
    assert _accepted(d) == 2, (
        "the rejected delivery started a cooldown and swallowed the real one")


# ── the end-of-run rule is now unreachable, and the profile is clean ────────

def test_the_finished_sample_set_is_always_one_family():
    d = _dlg()
    for src in (SOURCE_TOUCH, SOURCE_TABLET, SOURCE_TOUCH, SOURCE_MOUSE,
                SOURCE_TOUCH, SOURCE_TABLET, SOURCE_TOUCH, SOURCE_TOUCH,
                SOURCE_TOUCH):
        _tap(d, _touch(src))
    assert d.completed, "a run peppered with strays never finished"
    assert {s.source for s in d._samples} == {SOURCE_TOUCH}
    assert _accepted(d) == 5


def test_a_run_full_of_strays_never_reports_a_mixed_set_failure():
    """The old end-of-run rejection message must not appear any more — it is
    a backstop now, and a user seeing it would mean the gate leaked."""
    d = _dlg()
    for src in (SOURCE_TOUCH, SOURCE_TABLET, SOURCE_TOUCH, SOURCE_TOUCH,
                SOURCE_MOUSE, SOURCE_TOUCH, SOURCE_TOUCH):
        _tap(d, _touch(src))
    assert "MORE THAN ONE" not in d._instruction.text(), d._instruction.text()


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)
