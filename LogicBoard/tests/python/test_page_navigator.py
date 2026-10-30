"""
test_page_navigator.py — the [Prev] [N / Total] [Next] cluster.

    python tests/python/test_page_navigator.py

  * the counter is a tile of its own, centred on the row, with no caption;
  * it is sized for the number: about two thirds of a button's height;
  * its corners are round at every toolbar scale — a radius over half the
    tile's height makes Qt draw square corners;
  * THE NUMBER NEVER CLIPS, however many pages the board has: the tile is
    sized from the label's real font, and a huge count shrinks the font
    rather than the toolbar growing without end.
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Real Windows fonts: offscreen Qt otherwise falls back to a font whose
# every glyph is the same box, and widths measured in it mean nothing.
if os.path.isdir("C:/Windows/Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

_app = QApplication.instance() or QApplication([])

from ui.app import _global_stylesheet
from ui.toolbar.page_navigator import PageNavigator, _MAX_W

# The app's own stylesheet (it sets the font family), as in the real window.
_app.setStyleSheet(_global_stylesheet())

SCALES = (0.7, 0.85, 1.0, 1.25, 1.4, 2.0)
COUNTS = ("01 / 01", "09 / 12", "100 / 250", "999 / 999", "1000 / 1000",
          "12345 / 99999", "1234567 / 9999999")


def _nav(scale, text="01 / 01"):
    host = QWidget()
    QHBoxLayout(host).addWidget(nav := PageNavigator())
    nav.apply_scale(scale)
    nav.set_page_text(text)
    host.adjustSize()
    host.show()
    for _ in range(3):
        _app.processEvents()
    return host, nav


def test_there_is_no_page_caption():
    _host, nav = _nav(1.0)
    texts = [w.text() for w in nav.findChildren(QLabel)]
    assert texts == ["01 / 01"], texts


def test_the_tile_is_centred_and_sized_for_the_number():
    for sc in SCALES:
        host, nav = _nav(sc)
        f, b = nav._center_frame, nav.prev_btn
        fc = f.mapTo(host, f.rect().center()).y()
        bc = b.mapTo(host, b.rect().center()).y()
        assert abs(fc - bc) <= 1, (sc, fc, bc)
        ratio = f.height() / b.height()
        assert 0.55 <= ratio <= 0.75, "scale %s: tile is %.2f of a button" % (
            sc, ratio)
        # Even room above and below the digits, not crammed.
        line = QFontMetrics(nav._value.font()).height()
        assert f.height() - line >= nav._s(10), (sc, f.height(), line)


def test_the_tile_corners_are_round_at_every_scale():
    for sc in SCALES:
        _host, nav = _nav(sc)
        f = nav._center_frame
        r = int(re.search(r"PageNavIndicator \{[^}]*border-radius: (\d+)px",
                          f.styleSheet()).group(1))
        assert r > 0 and 2 * r <= f.height(), (sc, r, f.height())


def test_the_number_never_clips():
    for sc in SCALES:
        for text in COUNTS:
            _host, nav = _nav(sc, text)
            lab = nav._value
            need = QFontMetrics(lab.font()).horizontalAdvance(text)
            have = lab.contentsRect().width()
            assert need <= have, (
                "scale %s, %r: %d px of text in a %d px label"
                % (sc, text, need, have))


def test_paging_does_not_shuffle_the_buttons():
    _host, nav = _nav(1.0, "01 / 12")
    w = nav._center_frame.width()
    for text in ("05 / 12", "09 / 12", "10 / 12", "12 / 12"):
        nav.set_page_text(text)
        assert nav._center_frame.width() == w, text


def test_a_huge_board_shrinks_the_font_not_the_toolbar():
    _host, nav = _nav(1.0, "123456 / 999999")
    assert nav._center_frame.width() <= nav._s(_MAX_W) + 1
    _host, small = _nav(1.0, "01 / 01")
    assert nav._value_px < small._value_px


def test_a_short_board_shrinks_back():
    _host, nav = _nav(1.0, "1000 / 1000")
    wide = nav._center_frame.width()
    nav.set_page_text("01 / 01")
    assert nav._center_frame.width() < wide


def test_tapping_the_counter_opens_the_pages():
    _host, nav = _nav(1.0)
    hits = []
    nav.center_clicked.connect(lambda: hits.append(1))
    nav._center_frame.clicked.emit()
    assert hits == [1]


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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
