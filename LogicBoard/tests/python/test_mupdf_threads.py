"""
test_mupdf_threads.py — MuPDF is never entered by two threads at once.

Runs under pytest, and also standalone
(``python tests/python/test_mupdf_threads.py``).

PyMuPDF documents that it does not support multithreading and that doing
so "may cause incorrect behaviour or even crash Python itself". The import
pipeline renders on a thread pool; the suspected cause of LogicBoard
terminating mid-import on a school board (24 threads, never on the 12-thread
development laptop) was exactly that. The fix is one lock around every
MuPDF call, with the WEBP encoding still parallel.

This test counts how many threads are inside a MuPDF call at the same
moment during a real many-threaded render, and requires that it is never
more than one — while still requiring that the render WAS parallel.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _sample_pdf(tmp: Path, pages: int = 24) -> Path:
    real = ROOT.parent / "sample-100pages.pdf"
    if real.is_file():
        return real
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page()
        for k in range(40):
            pg.insert_text((40, 40 + k * 18), "Line %d of page %d — text" % (k, i))
    p = tmp / "s.pdf"
    doc.save(str(p))
    return p


def test_no_two_threads_are_ever_inside_mupdf_at_once():
    try:
        import fitz
    except ImportError:
        return
    import ui.util.media_cache as mc

    inside = [0]
    peak = [0]
    threads_seen = set()
    guard = threading.Lock()

    def watch(fn):
        def wrapper(*a, **k):
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
                threads_seen.add(threading.get_ident())
            try:
                time.sleep(0.0005)          # widen any overlap window
                return fn(*a, **k)
            finally:
                with guard:
                    inside[0] -= 1
        return wrapper

    saved = (fitz.Document.load_page, fitz.Page.get_pixmap,
             mc._render_worker_count)
    fitz.Document.load_page = watch(fitz.Document.load_page)
    fitz.Page.get_pixmap = watch(fitz.Page.get_pixmap)
    mc._render_worker_count = lambda n, px=None: 12
    try:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            n, ordered, _ = mc.render_pdf_pages(_sample_pdf(d), d / "out", 1280)
        assert n > 0 and all(p and t for p, t, _h in ordered), \
            "pages failed to render under the lock"
        assert len(threads_seen) > 1, "the render was not parallel at all"
        assert peak[0] == 1, (
            "%d threads were inside MuPDF at the same moment — PyMuPDF does "
            "not support that and it can kill the process" % peak[0])
    finally:
        fitz.Document.load_page, fitz.Page.get_pixmap, \
            mc._render_worker_count = saved


def test_the_encode_stays_outside_the_lock():
    """The lock must not serialise the expensive half: WEBP encoding."""
    import inspect
    import ui.util.media_cache as mc
    src = inspect.getsource(mc._PageRenderer.render)
    lock_block = src[src.index("with _MUPDF_LOCK:"):src.index("if img is None:")]
    assert "_save_webp" not in lock_block, \
        "WEBP encoding happens while holding the MuPDF lock"


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
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
