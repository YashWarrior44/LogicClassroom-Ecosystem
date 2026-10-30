"""
test_media_render.py — media that didn't render can be rendered in place,
and a missing PowerPoint is named as the reason.

    python tests/python/test_media_render.py

1. ``have_powerpoint`` used to check only that pywin32 and Pillow imported,
   which is true on every build — so a board without PowerPoint attempted
   the render, failed to start PowerPoint, and reported "couldn't read any
   slides". It now reads PowerPoint's automation registration.
2. Why a render produced nothing is recorded on the item and saved with the
   board, so the Media Manager can say "needs PowerPoint" later.
3. The Media Manager offers Render for unrendered media, which rebuilds the
   pages from the original already on the board — no second import.
4. "Remove From Board" is yellow (keeps the media), "Delete Media" red.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.util import ppt_renderer
from ui.util.media_library import (MediaLibrary, RENDER_ERR_FAILED,
                                   RENDER_ERR_NO_POWERPOINT)


class _Swap:
    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.saved = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)
        return False


def _board():
    d = Path(tempfile.mkdtemp(prefix="lb_render_"))
    return d, MediaLibrary(d)


def _pdf(path: Path, pages=3) -> Path:
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=400, height=300)
        pg.insert_text((40, 80), f"Page {i + 1}", fontsize=28)
    doc.save(str(path))
    doc.close()
    return path


# ── 1. the PowerPoint check is real ─────────────────────────────────────────

def test_no_powerpoint_registration_means_no_powerpoint():
    import winreg

    def _missing(*a, **k):
        raise OSError("not registered")
    with _Swap(winreg, OpenKey=_missing):
        assert ppt_renderer.powerpoint_installed() is False
        assert ppt_renderer.have_powerpoint() is False, \
            "have_powerpoint still says yes without PowerPoint installed"


def test_this_machine_answers_without_launching_anything():
    assert ppt_renderer.powerpoint_installed() in (True, False)


# ── 2. the reason is recorded and saved ─────────────────────────────────────

def test_a_presentation_imported_without_powerpoint_says_so():
    d, lib = _board()
    src = d / "lesson.pptx"
    src.write_bytes(b"PK\x03\x04 not really a deck")
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        item = lib.import_file(src)
    assert item is not None, "the original should still be kept on the board"
    assert item.preview_count == 0 and item.needs_render
    assert item.render_error == RENDER_ERR_NO_POWERPOINT
    # Saved with the board.
    again = MediaLibrary(d)
    again.load()
    assert again.get(item.id).render_error == RENDER_ERR_NO_POWERPOINT
    shutil.rmtree(d, ignore_errors=True)


# ── 3. Render rebuilds from the stored original ────────────────────────────

def test_an_unrendered_pdf_renders_from_the_original_on_the_board():
    d, lib = _board()
    src = _pdf(d / "notes.pdf")

    def _broken(self, item, *a, **k):
        item.preview_count = 0
        item.render_error = RENDER_ERR_FAILED
    with _Swap(MediaLibrary, _build_pdf_cache=_broken):
        item = lib.import_file(src)
    assert item.needs_render and item.render_error == RENDER_ERR_FAILED
    os.remove(src)            # Render must NOT need the file it came from

    from ui.util.import_progress import RenderWorker
    original = lib.absolute_path(item.original_relpath)
    w = RenderWorker(lib, original, item_id=item.id)
    done, failed = [], []
    w.finished_ok.connect(done.append)
    w.failed.connect(failed.append)
    w.run()                   # synchronously, on this thread
    item = lib.get(item.id)
    assert not failed, failed
    assert done == [item.id]
    assert item.preview_count == 3 and not item.needs_render
    assert item.render_error == ""
    shutil.rmtree(d, ignore_errors=True)


def test_rendering_a_presentation_without_powerpoint_reports_it():
    d, lib = _board()
    src = d / "deck.pptx"
    src.write_bytes(b"PK\x03\x04 deck")
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        item = lib.import_file(src)
        from ui.util.import_progress import RenderWorker
        w = RenderWorker(lib, lib.absolute_path(item.original_relpath),
                         item_id=item.id)
        failed = []
        w.failed.connect(failed.append)
        w.run()
    assert failed == [RENDER_ERR_NO_POWERPOINT], failed
    shutil.rmtree(d, ignore_errors=True)


# ── the Media Manager ──────────────────────────────────────────────────────

def _panel_with(item):
    from ui.panels.media_manager import MediaActionsPanel
    p = MediaActionsPanel()
    p.show()
    p.set_item(item, -1)
    _app.processEvents()
    return p


def test_render_is_offered_only_for_unrendered_media():
    d, lib = _board()
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        src = d / "deck.pptx"
        src.write_bytes(b"PK\x03\x04 deck")
        bad = lib.import_file(src)
    good = lib.import_file(_pdf(d / "fine.pdf"))
    p = _panel_with(bad)
    assert not p._render_btn.isHidden(), "no Render for an unrendered deck"
    assert "PowerPoint" in p._render_note.text()
    assert not p._btn_by_label["Add To Board"].isEnabled(), \
        "Add To Board offered for media with no pages"
    assert p._btn_by_label["Remove From Board"].isEnabled()
    assert p._btn_by_label["Delete Media"].isEnabled()
    p.set_item(good, -1)
    assert p._render_btn.isHidden(), "Render shown for media that rendered"
    assert p._btn_by_label["Add To Board"].isEnabled()
    shutil.rmtree(d, ignore_errors=True)


def test_the_render_button_asks_the_dialog_to_render():
    d, lib = _board()
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        src = d / "deck.pptx"
        src.write_bytes(b"PK\x03\x04 deck")
        bad = lib.import_file(src)
    p = _panel_with(bad)
    asked = []
    p.render_requested.connect(asked.append)
    p._render_btn.click()
    assert asked == [bad.id]
    shutil.rmtree(d, ignore_errors=True)


def test_the_library_row_says_needs_powerpoint():
    from ui.panels.media_manager import MediaCard
    from PySide6.QtWidgets import QLabel
    d, lib = _board()
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        src = d / "deck.pptx"
        src.write_bytes(b"PK\x03\x04 deck")
        bad = lib.import_file(src)
    card = MediaCard(bad, library=lib)
    texts = " ".join(l.text() for l in card.findChildren(QLabel))
    assert "needs PowerPoint" in texts, texts
    shutil.rmtree(d, ignore_errors=True)


def test_remove_from_board_is_yellow_and_delete_is_red():
    from ui.theme.theme_manager import theme_mgr
    t = theme_mgr.tokens
    p = _panel_with(None)
    assert t.warning_bg in p._btn_by_label["Remove From Board"].styleSheet()
    assert t.danger_bg in p._btn_by_label["Delete Media"].styleSheet()


# ── the alerts name PowerPoint ─────────────────────────────────────────────

def test_the_render_action_without_powerpoint_says_powerpoint_is_needed():
    import ui.main_window as mw
    d, lib = _board()
    with _Swap(ppt_renderer, powerpoint_installed=lambda: False):
        src = d / "deck.pptx"
        src.write_bytes(b"PK\x03\x04 deck")
        bad = lib.import_file(src)
        said = []
        fake = type("W", (), {})()
        fake._media_library = lambda: lib
        fake._input_cal = type("C", (), {"ppt_animations": False})()
        with _Swap(mw.AeroAlert,
                   warning=staticmethod(lambda *a, **k: said.append(a[2]))):
            mw.MainWindow._media_render(fake, bad.id)
    assert said and "needs Microsoft PowerPoint" in said[0], said
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
