"""
test_uniformity.py — one palette, and it is the one in class T.

Run it with ``python test_uniformity.py``.

What is worth protecting here
-----------------------------
``class T`` is this app's design tokens, and it is good. The problem was
everything outside it. A count of the colour literals in LogicShare.py that
were NOT in T came to ninety-one, and they fell into obvious families:

  * **Ten white-fill alphas** — 10, 12, 14, 16, 18, 20, 24, 28, 38 and 40
    out of 255 — for what are really four states. Ten steps is more than the
    eye can distinguish and exactly enough for two controls doing the same
    job to end up different: a text input filled at 16 hovering to 24, next
    to a button filled at 18 hovering to 38.

  * **Twelve opaque greys** — #2B3138, #3E4349, #41464D, #43484E, #454A50,
    #494E54, #4E5259, #575B61, #60646A, #65696E, #696D72, #7B7F83 — several
    of them four units apart. Four units is far too close to be a decision
    and far too scattered to be an accident.

  * **Six blues in the primary button**, in an app whose accent ramp is
    exactly three colours, none of which the button used.

  * **The Read chip**, drawing ``#6ccf80`` text on an ``rgba(108,203,95)``
    background — two different greens, and the second one is LogicBoard's
    success colour, which had wandered across from the other app.

THE RULE
========
A colour in this file is either in ``class T``, or DERIVED from something in
``class T`` by ``rgba()`` / ``shade()``, or on the allowlist below with a
reason. "Correct today" is not a category — ``rgba(76,194,255,72)`` was
correct today, and nothing connected it to ``T.ACCENT``, so it would have
gone quietly wrong the first time the accent moved.
"""
import io
import os
import re
import sys
import tokenize

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QWidget

_app = QApplication.instance() or QApplication([])

import LogicShare as LS

_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "LogicShare.py")
_SRC = io.open(_SRC_PATH, encoding="utf-8").read()
_LINES = _SRC.split("\n")
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(\s*\d")

# Line ranges whose literals are deliberate, and why.
_ALLOWED_PATTERNS = {
    r'\("#[0-9a-f]{6}", "#[0-9a-f]{6}"\)':
        "the seven avatar gradient pairs — deliberate variety, so two "
        "classrooms are told apart at a glance",
    r'QColor\("#ffffff"\)':
        "white text and tick marks drawn ON the avatar gradients, which can "
        "be any of the seven",
    r'color="#6ccf80"':
        "a default argument that names T.SUCCESS; see the dedicated test",
}


def _class_t_range():
    start = next(i for i, l in enumerate(_LINES) if l.startswith("class T:"))
    end = next(i for i in range(start + 1, len(_LINES))
               if _LINES[i] and not _LINES[i][0].isspace())
    return start, end


def _allowed(line: str) -> bool:
    return any(re.search(p, line) for p in _ALLOWED_PATTERNS)


# ── the palette ─────────────────────────────────────────────────────────────

def test_colours_live_in_class_T_or_are_derived_from_it():
    t0, t1 = _class_t_range()
    offenders = []
    for i, line in enumerate(_LINES):
        if t0 <= i < t1:
            continue                        # the palette itself
        st = line.strip()
        if st.startswith("#") or _allowed(line):
            continue
        if _COLOUR.search(line):
            offenders.append("LogicShare.py:%d  %s" % (i + 1, st[:84]))
    assert not offenders, (
        "%d colour literals outside class T. Put them in T, derive them with "
        "rgba()/shade(), or add a pattern to _ALLOWED_PATTERNS with a "
        "reason:\n  %s" % (len(offenders), "\n  ".join(offenders[:25])))


def test_the_fill_scale_has_four_steps_not_ten():
    """PRESSED equals SUBTLE on purpose — a Fluent control gets QUIETER
    under the finger, which is the opposite of what hover does."""
    T = LS.T
    steps = [T.FILL_SUBTLE, T.FILL, T.FILL_HOVER, T.FILL_ACTIVE]
    alphas = [int(re.search(r",(\d+)\)$", v).group(1)) for v in steps]
    assert alphas == sorted(alphas) and len(set(alphas)) == 4, \
        "the fill scale is %s; it must be four increasing steps" % alphas
    assert T.FILL_PRESSED == T.FILL_SUBTLE, \
        "pressed has stopped dipping below the rest state"


def test_the_stroke_scale_gets_brighter_in_order():
    T = LS.T
    ladder = [T.STROKE_SOFT, T.STROKE, T.STROKE_HI, T.STROKE_BRIGHT]
    lum = [sum(LS._channels(c)) for c in ladder]
    assert lum == sorted(lum) and len(set(lum)) == 4, \
        "the stroke ladder is out of order or has duplicates: %s" % ladder
    # Far enough apart to be told apart. The old ladder had steps four
    # units wide, which is a difference nobody can see and everybody can
    # accidentally create.
    gaps = [lum[i + 1] - lum[i] for i in range(3)]
    assert min(gaps) >= 30, \
        "stroke steps %s are too close to be distinguishable" % gaps


# ── derived, not picked ─────────────────────────────────────────────────────

def test_rgba_builds_from_a_token_rather_than_repeating_its_channels():
    assert LS.rgba(LS.T.ACCENT, 72) == "rgba(76,194,255,72)"
    assert LS.rgba(LS.T.ACCENT_HI, 110) == "rgba(124,208,255,110)"


def test_shade_moves_toward_white_and_black():
    up = LS.shade(LS.T.ACCENT_HI, 0.14)
    down = LS.shade(LS.T.ACCENT_HI, -0.20)
    base = LS._channels(LS.T.ACCENT_HI)
    assert all(a >= b for a, b in zip(LS._channels(up), base)), "shade(+) darkened"
    assert all(a < b for a, b in zip(LS._channels(down), base)), "shade(-) lightened"
    assert LS.shade(LS.T.ACCENT, 0.0) == LS.T.ACCENT.lower()


def test_the_primary_button_is_painted_in_the_accent_ramp():
    """It used to carry six blues — #6ED2FF/#38A0EB rest, #82DCFF/#46B4FF
    hover, #3CA0E6/#2882C8 pressed — and not one of them was T.ACCENT,
    T.ACCENT_HI or T.ACCENT_LO."""
    css = LS.ACCENT_BTN_CSS
    for gone in ("110,210,255", "56,160,235", "130,220,255", "70,180,255",
                 "60,160,230", "40,130,200"):
        assert gone not in css, "the primary button still uses rgba(%s)" % gone
    assert LS.rgba(LS.T.ACCENT_HI, 235) in css, "rest state is not ACCENT_HI"
    assert LS.rgba(LS.T.ACCENT_LO, 230) in css, "rest state is not ACCENT_LO"


def test_the_pressed_button_matches_the_android_build():
    """Both apps darken the accent ramp by 20%. This is the check that the
    two LogicShare versions actually agree, rather than agreeing by having
    been given the same hand-picked numbers once."""
    assert LS.shade(LS.T.ACCENT_HI, -0.20) == "#63a6cc"
    assert LS.shade(LS.T.ACCENT_LO, -0.20) == "#1e78b6"


def test_every_status_chip_is_one_colour():
    """The Read chip had ``#6ccf80`` text on an ``rgba(108,203,95)``
    background: two greens, and the second was LogicBoard's."""
    wrong = []
    for _state, (fg, bg, label) in LS.ACK_STATE_STYLE.items():
        if fg == LS.T.TEXT_MUTED:
            continue                        # Offline is deliberately neutral
        want = LS._channels(fg)
        m = re.match(r"rgba\((\d+),(\d+),(\d+),", bg)
        got = tuple(int(m.group(i)) for i in (1, 2, 3)) if m else None
        if got != want:
            wrong.append("%s: text %s, background %s" % (label, fg, bg))
    assert not wrong, "chips whose background is a different colour from " \
                      "their text: " + "; ".join(wrong)


def test_the_success_default_argument_is_the_success_token():
    """A default of ``color="#6ccf80"`` is T.SUCCESS spelled out. It is on
    the allowlist, so this is what actually holds it to the token."""
    import inspect
    for _name, obj in vars(LS).items():
        if not inspect.isclass(obj):
            continue
        init = getattr(obj, "__init__", None)
        if init is None or not callable(init):
            continue
        try:
            sig = inspect.signature(init)
        except (ValueError, TypeError):
            continue
        p = sig.parameters.get("color")
        if p is None or p.default is inspect.Parameter.empty:
            continue
        if isinstance(p.default, str) and p.default.startswith("#"):
            assert p.default.lower() == LS.T.SUCCESS.lower(), (
                "%s(color=%r) is a colour literal that is no longer T.SUCCESS"
                % (obj.__name__, p.default))


# ── elevated surfaces ───────────────────────────────────────────────────────

def test_there_are_three_surfaces_not_ten():
    """Six qlineargradients and four flat fills, all variations on three
    things: a chrome bar, a raised card, and a list tile. Two of the card
    pairs differed by FIVE units of alpha out of 255."""
    T = LS.T
    for name in ("SURF_BAR", "SURF_CARD", "SURF_CARD_SEL"):
        top, bottom = getattr(T, name)
        assert len(top) == len(bottom) == 4, "%s is not (rgba, rgba)" % name
        # Lit from above: the top stop is lighter than the bottom one. A
        # surface gradient running the other way looks lit from underneath
        # while every other surface in the app is lit from above.
        assert sum(top[:3]) > sum(bottom[:3]), \
            "%s runs dark to light — it will look lit from below" % name


def test_a_card_sits_above_a_bar():
    """The elevation order has to hold, or a raised card reads as a recess.
    Every surface is solid now (there is no blur behind the window), so
    LIGHTNESS is the depth cue: window body < chrome bar < card."""
    T = LS.T
    body = sum(T.BODY[0])
    bar = sum(T.SURF_BAR[0][:3])
    card = sum(T.SURF_CARD[0][:3])
    assert body < bar < card, \
        "elevation out of order: body %d, bar %d, card %d" % (body, bar, card)
    for name in ("SURF_BAR", "SURF_CARD", "SURF_CARD_SEL", "SURF_TILE"):
        v = getattr(T, name)
        alphas = [v[3]] if len(v) == 4 and isinstance(v[0], int) else [v[0][3], v[1][3]]
        assert all(a == 255 for a in alphas), \
            "%s is translucent — there is no blur behind it to show" % name


def test_the_selected_card_is_the_only_tinted_one():
    """Selection is signalled with a hue shift toward the accent. The other
    surfaces must stay neutral, or the tint stops meaning anything."""
    T = LS.T
    sel_top = T.SURF_CARD_SEL[0]
    assert sel_top[2] > sel_top[0] * 2, "the selected card is not blue-tinted"
    for name in ("SURF_BAR", "SURF_CARD"):
        top = getattr(T, name)[0]
        assert top[2] < top[0] * 2.2, \
            "%s is as blue as the selected state — selection stops reading" % name


def test_a_board_tile_is_one_colour_in_both_modes():
    """THE clearest case of the ten. The same tile painted itself
    rgba(24,30,38,180) normally and rgba(30,35,45,150) in selection mode —
    two different shades for one surface. Turning selection mode on changes
    the border and shows a checkbox; it is not a different card, and the
    tile should not change shade underneath the user.
    """
    T = LS.T
    assert len(T.SURF_TILE) == 4 and len(T.SURF_TILE_HOVER) == 4
    assert sum(T.SURF_TILE_HOVER[:3]) > sum(T.SURF_TILE[:3]), \
        "hovering a board tile makes it darker"
    src = _SRC
    # Both branches must reference the same token.
    assert src.count("{fill(T.SURF_TILE)}") == 2, \
        "the two board-tile rest states have diverged again"
    assert src.count("{fill(T.SURF_TILE_HOVER)}") == 2, \
        "the two board-tile hover states have diverged again"


def test_vgrad_builds_what_the_stylesheets_expect():
    g = LS.vgrad((1, 2, 3, 4), (5, 6, 7, 8))
    assert g.startswith("qlineargradient(x1:0,y1:0,x2:0,y2:1,")
    assert "stop:0 rgba(1,2,3,4)" in g and "stop:1 rgba(5,6,7,8)" in g
    assert LS.fill((9, 8, 7, 6)) == "rgba(9,8,7,6)"


# ── nothing was broken on the way ───────────────────────────────────────────

def test_no_token_reference_landed_in_a_plain_string():
    """``"QFrame { background: {T.FILL} }"`` — a token reference in a
    non-f-string — is legal Python, reads fine, and reaches Qt as the
    literal text ``{T.FILL}``, at which point Qt discards the whole rule
    silently. There is no error and no warning; the control simply loses
    its background. One of these was created while collapsing the palette
    and this is the check that caught it.
    """
    stranded = []
    with io.open(_SRC_PATH, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type != tokenize.STRING:
                continue
            head = tok.string[:3].lower()
            if head.startswith(("f", "rf", "fr")):
                continue
            if head.startswith(('"""', "'''")):
                continue                    # a docstring, not a stylesheet
            if re.search(r"\{T\.[A-Z_]+\}|\{rgba\(|\{shade\(", tok.string):
                stranded.append("line %d: %s" % (tok.start[0],
                                                 tok.string.strip()[:80]))
    assert not stranded, ("token references in non-f-strings:\n  "
                          + "\n  ".join(stranded))


def test_no_stylesheet_reaches_a_widget_with_unresolved_braces():
    """The same failure, checked from the other end: build everything that
    can be built and read back what Qt was actually handed."""
    stray = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.(), ]*\}")
    problems, built, styled = [], 0, 0
    for name in sorted(dir(LS)):
        obj = getattr(LS, name)
        if not (isinstance(obj, type) and issubclass(obj, QWidget)
                and getattr(obj, "__module__", "") == "LogicShare"):
            continue
        try:
            w = obj()
        except Exception:
            continue                        # needs arguments; covered by
        built += 1                          # test_announcements.py instead
        seen, stack = set(), [w]
        while stack:
            c = stack.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            ss = c.styleSheet()
            if ss:
                styled += 1
                m = stray.search(ss)
                if m:
                    problems.append("%s -> %s" % (c.__class__.__name__,
                                                  m.group(0)))
            stack.extend(k for k in c.children() if isinstance(k, QWidget))
    assert built >= 8, "only %d widget classes could be built" % built
    assert styled >= 30, "only %d stylesheets seen — coverage collapsed" % styled
    assert not problems, "unresolved stylesheets: " + "; ".join(problems[:6])


def test_module_level_stylesheets_are_fully_resolved():
    bad = []
    for name in dir(LS):
        obj = getattr(LS, name)
        if isinstance(obj, str) and name.endswith("_CSS"):
            m = re.search(r"\{[A-Za-z_]", obj)
            if m:
                bad.append(name)
    assert not bad, "unresolved module-level stylesheets: " + ", ".join(bad)


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("  ok    " + name)
        except AssertionError as exc:
            failures.append(name)
            print("  FAIL  %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            failures.append(name)
            print("  ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
    print(("\n%d failed" % len(failures)) if failures else "\nall passed")
    sys.exit(1 if failures else 0)
