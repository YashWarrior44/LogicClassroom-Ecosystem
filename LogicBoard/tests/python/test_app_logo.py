"""
test_app_logo.py — the app logo is drawn whole, at every scale.

    python tests/python/test_app_logo.py

Why this file exists
--------------------
``app_logo_pixmap`` composites the artwork onto a square canvas allocated in
REAL pixels. ``QIcon.pixmap()`` multiplies the size you ask for by the
application's device-pixel ratio and hands back a pixmap tagged with it — so
``icon.pixmap(QSize(64, 64))`` at 125 % scaling is 80x80 real pixels
reporting a logical extent of 64.

The centring offset read ``.width()`` (real, 80) and ``drawPixmap`` placed
the art at its logical extent (64). ``(64 - 80) // 2`` is -8, so the logo sat
eight pixels up and to the left of its canvas with the top-left corner cut
flat and nine pixels of dead space down the right. Measured on the real
icon at the four sizes the app requests: three of them clipped.

It is invisible at 100 % scaling, and every existing logo test ran there.
Those tests checked the pixmap's SIZE and its ratio tag, both of which were
correct the whole time — the defect was in where the ink landed inside it.
So this file forces a fractional ratio, in a subprocess so the setting
cannot leak into other tests, and looks at the pixels.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# Sizes are derived INSIDE the probe from the icon's own frames and the
# live ratio, not from UiScale.
#
# The first version of this file asked for UiScale.px(42/84/56/72) and
# caught nothing. UiScale keys off screen geometry, and the offscreen
# platform this test runs on is not the real display: it turns 56 into 48
# where the real screen turns it into 51. Every requested size then landed
# on a real-pixel count that is NOT one of the authored frames (45, 89, 60,
# 76), so all four probes took the master-downscale branch — the branch
# that was never broken. The bug is in the exact-frame branch, and reaching
# it needs ``round(size * ratio)`` to hit 16/20/24/32/40/48/64/96/128/256
# exactly. So the probe works backwards from the frames instead.
#
# What is asserted is SYMMETRY, not a minimum margin. The small authored
# frames are drawn flush to the left and right on purpose — measured, 16,
# 20, 24 and 40 all have a zero side margin — so a "margin >= 1" rule would
# fail on correct art. Being off-centre is the defect either way: the
# original put 0 on the left against 9 on the right.

_PROBE = textwrap.dedent('''
    import sys, json
    sys.path.insert(0, __ROOT__)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import QSize
    app = QApplication([])
    from ui.util.icon_loader import app_logo_pixmap
    import ui.util.icon_loader as IL
    from ui.util.paths import find_app_icon_path

    def margins(pm):
        img = pm.toImage(); w, h = img.width(), img.height()
        ci = lambda x: any(img.pixelColor(x, y).alpha() > 12 for y in range(h))
        ri = lambda y: any(img.pixelColor(x, y).alpha() > 12 for x in range(w))
        L = next((x for x in range(w) if ci(x)), w)
        R = next((w - 1 - x for x in range(w - 1, -1, -1) if ci(x)), w)
        T = next((y for y in range(h) if ri(y)), h)
        B = next((h - 1 - y for y in range(h - 1, -1, -1) if ri(y)), h)
        return [L, R, T, B]

    ratio = float(app.devicePixelRatio())
    path = find_app_icon_path()
    out = {"dpr": ratio, "frames": [], "sites": []}
    if not path:
        print(json.dumps(out)); raise SystemExit(0)
    frames = sorted(f.width() for f in QIcon(str(path)).availableSizes())
    out["frames"] = frames

    wanted = []
    for f in frames:                     # exact-frame branch
        if f < 24:
            continue                     # too small to be a logo anywhere
        size = int(round(f / ratio))
        if size > 0 and int(round(size * ratio)) == f:
            wanted.append(("frame %d" % f, size))
    for size in (37, 53, 61, 71):        # master-downscale branch
        if int(round(size * ratio)) not in frames:
            wanted.append(("off-frame", size))

    for why, size in wanted:
        IL._logo_cache.clear()
        pm = app_logo_pixmap(size)
        if pm is None:
            out["sites"] = None
            break
        out["sites"].append({
            "why": why, "size": size, "phys": pm.width(),
            "dpr": pm.devicePixelRatio(), "margins": margins(pm)})
    print(json.dumps(out))
''')


def _probe(scale: str):
    """Run the logo through a real Qt app at ``scale`` and report pixels."""
    env = dict(os.environ)
    env["QT_SCALE_FACTOR"] = scale
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # A literal placeholder, not %-formatting: the probe body contains
    # its own %d format strings and they are not this function's to fill.
    src = _PROBE.replace("__ROOT__", repr(ROOT))
    r = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, f"probe at scale {scale} failed:\n{r.stderr}"
    import json
    line = [l for l in r.stdout.strip().splitlines() if l.startswith("{")]
    assert line, f"probe printed nothing usable:\n{r.stdout}\n{r.stderr}"
    return json.loads(line[-1])


def _check_centred(data, where):
    if data["sites"] is None or not data["sites"]:
        return                            # no icon in this tree
    for site in data["sites"]:
        L, R, T, B = site["margins"]
        assert abs(L - R) <= 1 and abs(T - B) <= 1, (
            "%s: logo at %dpx (%d real, %s) sits at L=%d R=%d T=%d B=%d — "
            "it is off-centre in its canvas and the long side is cut off"
            % (where, site["size"], site["phys"], site["why"], L, R, T, B))


def test_the_logo_is_centred_at_a_fractional_display_scale():
    """THE test. A fractional ratio is the only place real pixels and
    logical pixels stop being the same number, so it is the only place this
    can fail."""
    data = _probe("1.25")
    assert data["dpr"] > 1.01, (
        "QT_SCALE_FACTOR did not take — the ratio is %s, so this is "
        "checking the one case that never broke" % data["dpr"])
    _check_centred(data, "at 125%")


def test_it_is_still_right_at_plain_100_percent():
    """The fix rewrote the size request. The case that always worked has to
    keep working."""
    _check_centred(_probe("1"), "at 100%")


def test_the_probe_actually_reaches_the_branch_that_broke():
    """A coverage guard, and not a theoretical one — the first version of
    this file passed against the unfixed code because every size it asked
    for missed the exact-frame branch entirely. A test that cannot reach
    the defect is worse than no test: it reports safety it never checked.

    So assert the probe got there, and that it also covered the other
    branch."""
    data = _probe("1.25")
    if data["sites"] is None or not data["sites"]:
        return
    whys = [s["why"] for s in data["sites"]]
    exact = [w for w in whys if w.startswith("frame ")]
    other = [w for w in whys if w == "off-frame"]
    assert len(exact) >= 3, (
        "only %d exact-frame size(s) probed (%s) — the bug lived in that "
        "branch and this suite would not have seen it" % (len(exact), whys))
    assert other, "the master-downscale branch was never exercised"


def test_the_canvas_still_lays_out_at_the_size_that_was_asked_for():
    """The ratio tag is what keeps the pixmap from overflowing its label.
    Compositing in real pixels must not have dropped it."""
    data = _probe("1.25")
    if data["sites"] is None or not data["sites"]:
        return
    for site in data["sites"]:
        logical = site["phys"] / site["dpr"]
        assert abs(logical - site["size"]) <= 1, (
            "a %dpx request lays out at %.1fpx"
            % (site["size"], logical))


def test_the_about_section_in_settings_centres_its_logo_label():
    """A QLabel defaults to AlignLeft|AlignVCenter for a pixmap and does not
    scale one to fit, so a pixmap even a fraction wider than its label loses
    the overhang off the right. Settings asks for sc(56) — 51 here — and the
    ratio-tagged pixmap measures 51.2 logical."""
    src = os.path.join(ROOT, "ui", "panels", "settings_panel.py")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    head = text.index("def _build_about_page")
    body = text[head:head + 1600]
    assert "logo.setAlignment(Qt.AlignCenter)" in body, \
        "the About logo label is not centred; it will shave its right edge"


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
