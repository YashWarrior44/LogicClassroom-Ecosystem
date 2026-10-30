"""
test_ui_smoke.py — the app's foundations import and answer sensibly.

Runs under pytest, and also standalone
(``python tests/python/test_ui_smoke.py``) like every other suite here.
That mattered: the only pytest on this machine is 2.6.0, from 2014, and it
cannot even start under Python 3.11 — so for as long as this file needed
pytest to run, it did not run, and nobody found out.

What it had drifted into asserting
----------------------------------
``LIGHT_TOKENS``. LogicBoard is dark-only; the light palette was removed
along with the theme switch, so this file had been importing a name that no
longer exists. It would have failed on the first line of
``test_theme_tokens`` if anything had ever executed it.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])


def test_the_asset_root_resolves():
    from ui.util.paths import base_path
    assert base_path().exists(), "base_path() points at nothing on disk"


def test_ui_scaling_stays_in_a_sane_band():
    """UiScale multiplies every size in the app. A factor outside this band
    means a toolbar that either does not fit the screen or is unusable from
    the back of a classroom.

    Note what is NOT being tested. ``UiScale.factor`` still ACCEPTS a height
    and has ignored it since the logic moved into SizeManager — it always
    reports the scale for the screen the app is actually on. The version of
    this test that shipped read ``factor(1080)`` as "the factor at 1080p"
    and asserted it was at least 0.9; on this machine that call returns
    0.85, because it is answering about this screen and not about 1080p at
    all. It would have failed the first time it ran, which it never did.
    """
    from ui.util.scaling import UiScale
    from ui.util.size_manager import size_mgr
    f = UiScale.factor()
    assert 0.5 <= f <= 3.0, f"UI scale {f} would make the app unusable"
    assert UiScale.factor(720) == UiScale.factor(2160) == f, (
        "factor() has started honouring its height argument — that is an "
        "improvement, but every caller passes nothing, so update them too")
    assert f == size_mgr.ui_scale(), "the facade and SizeManager disagree"


def test_scaled_sizes_are_positive_whole_pixels():
    """``scale_size`` feeds setFixedSize and stylesheet px values. A zero
    collapses a control to nothing; a float lands in a stylesheet as
    '13.6px', which Qt drops silently along with the rest of the rule."""
    from ui.util.size_manager import size_mgr
    for value in (1, 2, 4, 8, 11, 13, 16, 24, 32, 40, 48):
        out = size_mgr.scale_size(value)
        assert isinstance(out, int), f"scale_size({value}) -> {out!r}"
        assert out >= 1, f"scale_size({value}) collapsed to {out}"


def test_the_theme_is_dark_and_only_dark():
    """The light palette and the theme switch were both removed. Anything
    that still expects a second theme is out of date — this file used to
    be, which is what hid it."""
    from ui.theme.tokens import DARK_TOKENS
    import ui.theme.tokens as tokens_mod
    assert DARK_TOKENS.name == "dark"
    assert DARK_TOKENS.is_dark and not DARK_TOKENS.is_light
    assert not hasattr(tokens_mod, "LIGHT_TOKENS"), (
        "a light palette is back; the tests and the theme manager both "
        "assume there is exactly one")


def test_the_theme_manager_hands_out_those_tokens():
    from ui.theme.theme_manager import theme_mgr
    from ui.theme.tokens import DARK_TOKENS
    assert theme_mgr.theme_name == "dark"
    assert theme_mgr.is_dark
    assert theme_mgr.tokens is DARK_TOKENS


def test_asking_for_a_theme_that_does_not_exist_is_survivable():
    """``set_theme`` is still public. It must not leave the app without a
    palette — every stylesheet in the UI reads through it."""
    from ui.theme.theme_manager import theme_mgr
    for name in ("light", "solarized", "", None):
        try:
            theme_mgr.set_theme(name)
        except Exception:
            pass
        assert theme_mgr.tokens is not None, f"set_theme({name!r}) left no tokens"
    theme_mgr.set_theme("dark")
    assert theme_mgr.is_dark


def test_every_token_colour_parses_into_a_real_qcolor():
    """``QColor(str)`` cannot parse ``rgb(...)``/``rgba(...)`` — it returns
    an INVALID colour, which Qt paints as solid black. That is a real bug
    this app has had (the selection rectangle came out pure black), so every
    colour token is checked through the parser that exists to prevent it."""
    from ui.theme.theme_manager import theme_mgr
    import dataclasses
    t = theme_mgr.tokens
    bad = []
    for field in dataclasses.fields(t):
        value = getattr(t, field.name)
        if not isinstance(value, str):
            continue
        if not (value.startswith(("rgb(", "rgba(", "#")) or value == "transparent"):
            continue                       # names like "dark" are not colours
        if value == "transparent":
            continue
        if not theme_mgr.qcolor(value).isValid():
            bad.append(f"{field.name} = {value!r}")
    assert not bad, "unparseable colour tokens (Qt paints these black): " + ", ".join(bad)


def test_the_type_scale_is_ordered_and_has_no_near_duplicates():
    """Two sizes one pixel apart read as a mistake, not a hierarchy — the
    reason the scale was introduced in the first place."""
    from ui.theme.theme_manager import theme_mgr
    t = theme_mgr.tokens
    scale = [t.font_caption, t.font_body, t.font_subtitle, t.font_title,
             t.font_display]
    assert scale == sorted(scale), f"type scale is out of order: {scale}"
    assert len(set(scale)) == len(scale), f"duplicate steps: {scale}"


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
