"""
test_ui_uniformity.py — one palette, one type scale, one of each control.

Runs under pytest, and also standalone
(``python tests/python/test_ui_uniformity.py``).

What is worth protecting here
-----------------------------
``ui/theme/tokens.py`` opens with a cardinal rule: *nothing else in the
codebase contains hex/rgb/rgba literals*. That rule had no enforcement, and a
rule with no enforcement is a comment. By the time this file was written the
app had accumulated:

  * a progress bar in ``#4f7cff -> #7eb0ff`` next to an accent of
    ``#4CC2FF`` — two blues close enough that they read as a mistake rather
    than as a second colour;
  * a save spinner in ``rgb(0,122,255)``, iOS system blue, a third one;
  * tooltips in ``#1c1c24 / #ececf2 / #3d3d4c``, all with a violet cast, in
    an app whose greys are neutral — and a tooltip appears over EVERY other
    surface, so that was the palette contradicting itself in the most
    visible place available;
  * a private grey ladder (``#e6e6e6 / #a8a8a8 / #cfcfcf / #6e6e6e``) shared
    between the two progress dialogs and nothing else;
  * ``hasattr(t, "warning_bg")`` guards with literal fallbacks — branches
    that can never be taken, whose only lasting effect was carrying a
    hardcoded amber past every search for one;
  * TWO scrollbar implementations, disagreeing about track width, handle
    length, track margin, and whether a pressed handle looked any different.

None of these were wrong on their own. That is exactly why they survived:
drift is not made of mistakes, it is made of small local decisions that were
each defensible at the time.

THE ALLOWLIST
=============
Some literals are genuinely intentional, and ``_ALLOWED`` names each one with
its reason. Adding to it is fine — writing down WHY is the part that matters,
because a bare exemption is indistinguishable from a bug next time somebody
reads this.
"""
import ast
import io
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage

_app = QApplication.instance() or QApplication([])

_UI = os.path.join(_ROOT, "ui")
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b|rgba?\(\s*\d")

# path suffix -> why its colour literals are allowed to stay.
_ALLOWED = {
    "theme/tokens.py":
        "the palette itself — this is where the literals are SUPPOSED to be",
    "panels/settings_panel.py":
        "six per-section identity hues, one per settings section. They are "
        "each section's own colour, not a restatement of a theme colour",
    "logicshare/notifications.py":
        "per-classroom avatar gradients — deliberate variety, so two "
        "classrooms are told apart at a glance — plus the white label text "
        "that sits on top of them",
    "canvas/selection_handles.py":
        "selection handles are white on purpose: they sit on top of the "
        "user's own ink, which can be any colour, including every theme one",
    "panels/pages_panel.py":
        "one white — the delete glyph, which sits on an accent chip and has "
        "to stay legible whatever the accent becomes. The card surfaces "
        "themselves used to be exempt here too, for being deliberately more "
        "translucent than bg_card; they are on the tokens now",
    "util/circle_slider.py":
        "a drop shadow. There is no shadow COLOUR token: elevation is "
        "expressed as (blur, dx, dy, alpha) and the colour is always black",
}


def _ui_sources():
    for root, dirs, files in os.walk(_UI):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in sorted(files):
            if fn.endswith(".py"):
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, _UI).replace(os.sep, "/")
                yield rel, path


def _code_only(src: str) -> str:
    """The source with comments and DOCSTRINGS removed — nothing else.

    Prose about a colour is not a use of it, and several of the most careful
    comments in this codebase quote the exact literal they are warning
    against. Flagging those would teach everyone to stop explaining.

    THE VERSION THIS REPLACES WAS BROKEN, AND QUIETLY SO.

    It stripped every triple-quoted string with a regex, on the assumption
    that a triple-quoted string is a docstring. In this codebase a
    triple-quoted string is usually a STYLESHEET — that is how every
    ``setStyleSheet`` call in the app is written — so the check that exists
    to find colour literals was skipping the exact place colour literals
    live. Verified against a sample: a stylesheet containing ``#ffffff``
    came out of the old filter with the literal gone.

    Docstrings are found with ``ast`` instead, which knows that a docstring
    is the first statement of a module, class or function and that every
    other string is a value. Only those lines are blanked.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    doc_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first, "end_lineno", first.lineno)
            doc_lines.update(range(first.lineno, end + 1))

    out = []
    for i, line in enumerate(src.split("\n"), 1):
        if i in doc_lines or line.strip().startswith("#"):
            out.append("")
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


def _scaled(v):
    from ui.util.size_manager import size_mgr
    return size_mgr.scale_px(v)


# ── the palette ─────────────────────────────────────────────────────────────

def test_no_colour_literals_outside_the_token_file():
    offenders = []
    for rel, path in _ui_sources():
        if any(rel.endswith(k) for k in _ALLOWED):
            continue
        code = _code_only(io.open(path, encoding="utf-8").read())
        for i, line in enumerate(code.split("\n"), 1):
            if _COLOUR.search(line):
                offenders.append("ui/%s:%d  %s" % (rel, i, line.strip()[:80]))
    assert not offenders, (
        "colour literals outside ui/theme/tokens.py — move them into the "
        "palette, or add the file to _ALLOWED with a reason:\n  "
        + "\n  ".join(offenders))


def test_the_allowlist_is_not_stale():
    """An exemption for a file that no longer needs one is how an allowlist
    quietly turns into a blanket."""
    unused = []
    for key in _ALLOWED:
        if key.endswith("tokens.py"):
            continue
        path = os.path.join(_UI, key)
        if not os.path.exists(path):
            unused.append(key + " (file is gone)")
            continue
        if not _COLOUR.search(_code_only(io.open(path, encoding="utf-8").read())):
            unused.append(key + " (has no literals any more)")
    assert not unused, "stale entries in _ALLOWED: " + ", ".join(unused)


def test_no_dead_hasattr_guards_around_theme_tokens():
    """``t.warning_bg if hasattr(t, 'warning_bg') else '#...'`` is a branch
    that can never be taken — the tokens are a frozen dataclass, so every
    field is always there. What it DID do is carry a literal past every
    search for one."""
    offenders = []
    for rel, path in _ui_sources():
        # Comments and docstrings only, because THIS file's own explanation
        # of the pattern quotes it verbatim, and so does the note left in
        # setup_wizard.py where the last one was removed.
        code = _code_only(io.open(path, encoding="utf-8").read())
        for i, line in enumerate(code.split("\n"), 1):
            if "hasattr(t," in line.replace(" ", ""):
                offenders.append("ui/%s:%d  %s" % (rel, i, line.strip()[:80]))
    assert not offenders, (
        "defensive hasattr() around theme tokens:\n  " + "\n  ".join(offenders))


def test_every_token_is_reachable_through_the_theme_manager():
    from ui.theme.theme_manager import theme_mgr
    from ui.theme.tokens import DARK_TOKENS
    assert theme_mgr.tokens is DARK_TOKENS


# ── one implementation per control ──────────────────────────────────────────

def test_there_is_exactly_one_scrollbar():
    """There were two, and they disagreed on four measurements. Nobody
    noticed because the copy in fluent_styles turned out to be DEAD — every
    scroll area in the app went through touch_scroll — so the divergence was
    invisible right up until somebody used the wrong one."""
    from ui.theme.fluent_styles import scrollbar
    from ui.theme.theme_manager import theme_mgr
    from ui.util.touch_scroll import _scrollbar_qss, canvas_scrollbar_qss
    a, b, c = _scrollbar_qss(), theme_mgr.scrollbar_stylesheet(), scrollbar()
    assert a == b == c, "the scrollbar stylesheets have diverged again"
    assert "pressed" in a, "the shared scrollbar lost its pressed state"
    assert "width: 18px" in canvas_scrollbar_qss(18), \
        "the canvas can no longer widen its bar for finger dragging"


def test_the_scrollbar_handle_is_solid_and_answers_a_grab():
    """Opaque in every state — a translucent handle vanished over the aero
    blur — and rest < hover in lightness, with pressed in the accent. A
    handle being dragged has a finger or a cursor on top of it, so the
    feedback has to survive being covered."""
    from ui.theme.theme_manager import theme_mgr
    t = theme_mgr.tokens
    cs = [theme_mgr.qcolor(v) for v in
          (t.scrollbar_handle, t.scrollbar_handle_hover,
           t.scrollbar_handle_pressed)]
    assert all(c.alpha() == 255 for c in cs), [c.alpha() for c in cs]
    assert cs[0].lightness() < cs[1].lightness(), "hover is not brighter"
    assert cs[2] != cs[0] and cs[2] != cs[1], "pressed looks like rest/hover"


def test_the_scrollbar_handle_has_round_ends():
    """Android-style pill: radius == half the handle's thickness."""
    import re
    from ui.theme.fluent_styles import scrollbar
    for w in (None, 18, 24):
        qss = scrollbar(w)
        track = int(re.search(r"QScrollBar:vertical \{[^}]*?width: (\d+)px",
                              qss).group(1))
        side = int(re.search(r"margin: \d+px (\d+)px", qss).group(1))
        radius = int(re.search(
            r"QScrollBar::handle:vertical \{[^}]*?border-radius: (\d+)px",
            qss).group(1))
        assert radius == (track - 2 * side) // 2, (w, track, side, radius)


def test_every_scroll_area_gets_the_shared_bar():
    """It is in the application stylesheet, so a widget that never asked
    for it does not fall back to the native Windows bar."""
    from ui.app import _global_stylesheet
    from ui.theme.fluent_styles import scrollbar
    assert scrollbar() in _global_stylesheet()


# ── the two progress surfaces are siblings ──────────────────────────────────

def test_the_save_and_import_dialogs_agree():
    """They appear seconds apart in the same workflow — import a PDF, then
    the board saves. They used to be visibly different objects: different
    greys, different blues, and one painted a 62%-opaque slab over the aero
    blur while the other let it through."""
    from ui.panels.saving_dialog import SavingDialog
    from ui.util.import_progress import ImportProgressDialog
    from ui.theme.theme_manager import theme_mgr
    t = theme_mgr.tokens

    save = SavingDialog(None, title="Saving", subtitle="...")
    imp = ImportProgressDialog(None, title="Importing", subtitle="...")
    title_px = str(_scaled(t.font_subtitle))
    sub_px = str(_scaled(t.font_caption))
    for name, title, sub in (("SavingDialog", save._title_label,
                              save._subtitle_label),
                             ("ImportProgressDialog", imp._title_label,
                              imp._status_label)):
        ts, ss = title.styleSheet(), sub.styleSheet()
        assert t.text_primary in ts, name + " title is not text_primary"
        assert t.text_secondary in ss, name + " subtitle is not text_secondary"
        assert title_px + "px" in ts, name + " title is off the type scale"
        assert sub_px + "px" in ss, name + " subtitle is off the type scale"


def test_neither_progress_surface_uses_a_second_blue():
    """The import bar was a violet-blue gradient and the save spinner was
    iOS blue, in an app whose accent is a cyan-blue."""
    from ui.panels.saving_dialog import SavingDialog
    from ui.util.import_progress import ImportProgressDialog
    from ui.theme.theme_manager import theme_mgr

    imp = ImportProgressDialog(None, title="x", subtitle="y")
    ss = imp._bar.styleSheet()
    assert theme_mgr.tokens.progress_chunk in ss
    for gone in ("#4f7cff", "#7eb0ff", "qlineargradient"):
        assert gone not in ss, "the progress bar still uses " + gone

    # The spinner is PAINTED, not styled, so check its pixels rather than
    # its stylesheet — there isn't one to read.
    save = SavingDialog(None, title="x", subtitle="y")
    sp = save._spinner
    sp.resize(60, 60)
    img = QImage(60, 60, QImage.Format_ARGB32)
    img.fill(0)
    sp.render(img)
    want = theme_mgr.qcolor(theme_mgr.tokens.accent)
    hits = 0
    for y in range(60):
        for x in range(60):
            c = img.pixelColor(x, y)
            if (c.alpha() > 40
                    and abs(c.red() - want.red()) < 14
                    and abs(c.green() - want.green()) < 14
                    and abs(c.blue() - want.blue()) < 14):
                hits += 1
    assert hits > 20, (
        "only %d accent pixels in the save spinner — it is painting in some "
        "other blue again" % hits)


# ── tooltips ────────────────────────────────────────────────────────────────

def test_tooltips_use_the_app_palette():
    """A tooltip appears over every other surface in the app, which makes it
    the easiest place for a palette to give itself away."""
    from ui.app import _global_stylesheet
    from ui.theme.theme_manager import theme_mgr
    t = theme_mgr.tokens
    qss = _global_stylesheet()
    assert t.bg_window in qss and t.text_primary in qss
    for gone in ("#1c1c24", "#ececf2", "#3d3d4c"):
        assert gone not in qss, "tooltips still use " + gone


def test_tool_icons_default_to_the_theme_glyph_colour():
    """The default used to be ``#e2e8f0``, a cool slate white, while the
    caption printed under every icon composited to a neutral grey. One or
    the other is fine; the two side by side is what reads as unfinished."""
    import inspect
    from ui.util.icon_loader import tool_icon
    from ui.theme.theme_manager import theme_mgr
    sig = inspect.signature(tool_icon)
    assert sig.parameters["color"].default is None, \
        "tool_icon has a hardcoded default colour again"
    assert theme_mgr.tokens.icon_default.startswith("#")
    assert tool_icon("pen", 32) is not None


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
