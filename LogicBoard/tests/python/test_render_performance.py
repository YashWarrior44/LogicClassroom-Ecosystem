"""
test_render_performance.py — the import must stay fast AND stay inside the
memory ceiling.

Runs under pytest, and also standalone
(``python tests/python/test_render_performance.py``).

These are not timing tests. A test that fails when the machine is busy is a
test people learn to ignore. They pin the DECISIONS the timings led to, each
of which is a number somebody could reasonably "clean up" without knowing
what it cost to arrive at.

WHAT WAS MEASURED
=================
Importing a real 1000-page PDF, and a 30-page A4 chapter, on a Ryzen 5
5600H (6 physical cores, 12 logical):

  * **Where the time goes.** Rasterising in MuPDF is 6.5 ms a page,
    copying it into Pillow 9.6 ms, and the WEBP encode 223 ms — 93% of the
    total. Python's own share is the six lines that call those libraries,
    which is why moving any of this into the C++ engine would rewrite the
    part that is not the cost.

  * **The encoder's effort setting.** method=0 renders the same page in
    148 ms against method=2's 222 ms, with a mean per-channel error of
    0.27 against 0.28 — 1.5x faster and, if anything, very slightly more
    faithful. It is the single biggest lever in the pipeline.

  * **Thumbnails.** Rendering one fresh from the page at 512 px takes
    31 ms; resizing the full 3840 px page down to 512 takes 141 ms for a
    byte-identical result. 4.5x, and it removes a large intermediate from
    the memory peak.

  * **The GIL.** ``pix.samples`` and ``Image.frombytes`` are buffer copies
    that do NOT release the GIL, unlike the library calls around them.
    Across a dozen worker threads those milliseconds serialise: a
    1000-page import sat at 61% of the machine until they were replaced
    with a memoryview, after which it reached 69%.

  * **Worker count.** Throughput peaks at roughly 1.5x the PHYSICAL core
    count and then falls: on this chip 10 workers took 2.90 s for 100
    pages while 12 took 3.05 s. Sizing the pool by ``os.cpu_count()`` —
    which reports LOGICAL processors — over-subscribes an SMT machine.

  * **Memory.** An in-flight page costs about 9.3 bytes per rendered
    pixel. At 3840 px that is 185 MB each, and eight at once put the peak
    working set at 1.6 GB — outside the 2.5 GB the app is meant to stay
    within. The pool is now sized by a memory budget as well.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QColor

_app = QApplication.instance() or QApplication([])

import ui.util.media_cache as mc
from ui.util.pixmap_cache import PixmapCache, default_budget_bytes

MB = 1024 * 1024
A4_1920 = 1920 * 2716
A4_3840 = 3840 * 5435


# ── the pixmap cache is bounded ─────────────────────────────────────────────

def _pixmap(mb):
    """A pixmap of roughly ``mb`` megabytes."""
    px = int((mb * MB / 4) ** 0.5)
    pm = QPixmap(px, px)
    pm.fill(QColor("white"))
    return pm


def test_the_cache_stays_inside_its_budget():
    """THE regression. It used to be a plain dict that nothing removed
    anything from: 8.3 MB per A4 page at 150 dpi, and the process's working
    set tracked it exactly — 75 MB with a board open, 275 MB after
    twenty-four pages."""
    cache = PixmapCache(budget_bytes=40 * MB)
    for i in range(30):
        cache["page_%d" % i] = _pixmap(8)
    assert cache.total_bytes <= 40 * MB, \
        "cache holds %.0f MB against a 40 MB budget" % (cache.total_bytes / MB)
    assert cache.evictions > 0, "nothing was ever evicted"


def test_it_keeps_what_was_used_most_recently():
    """A teacher moving through a document needs the pages around them, not
    the ones they started on."""
    cache = PixmapCache(budget_bytes=25 * MB)
    for i in range(5):
        cache["p%d" % i] = _pixmap(8)
    # Touch the oldest survivor so it is no longer the oldest.
    live = [k for k in list(cache.keys())]
    cache[live[0]]
    cache["new"] = _pixmap(8)
    assert live[0] in cache, "the most recently used entry was evicted"


def test_an_image_larger_than_the_whole_budget_is_still_returned():
    """A page that cannot be cached must still be drawable."""
    cache = PixmapCache(budget_bytes=4 * MB)
    big = _pixmap(20)
    cache["huge"] = big
    assert "huge" in cache, "an oversized page was dropped before it was drawn"
    got = cache["huge"]
    assert got.width() == big.width()


def test_the_oversized_image_does_not_stay_forever():
    cache = PixmapCache(budget_bytes=4 * MB)
    cache["huge"] = _pixmap(20)
    for i in range(6):
        cache["small_%d" % i] = _pixmap(1)
    assert "huge" not in cache, \
        "the oversized page is still resident after six others were cached"


def test_the_budget_is_a_share_of_the_machine_not_a_constant():
    """512 MB is most of a 2 GB classroom laptop and a rounding error on a
    32 GB desk machine."""
    budget = default_budget_bytes()
    assert 192 * MB <= budget <= 640 * MB, \
        "budget is %.0f MB, outside the sane band" % (budget / MB)


def test_the_canvas_uses_the_bounded_cache():
    import ui.canvas.canvas_widget as cw
    from ui.canvas.canvas_widget import CanvasWidget
    c = CanvasWidget()
    assert isinstance(c._pixmap_cache, PixmapCache), \
        "the canvas is back on a plain dict"


def test_clearing_the_cache_does_not_replace_it_with_a_dict():
    """main_window resets it when a board opens. Rebinding a ``{}`` there
    would silently remove the bound."""
    import inspect
    import ui.main_window as mw
    src = inspect.getsource(mw.MainWindow)
    assert "_pixmap_cache = {}" not in src, \
        "somewhere in main_window the cache is rebound to a plain dict"


# ── the worker pool ─────────────────────────────────────────────────────────

def test_the_pool_is_sized_by_physical_cores_not_logical_ones():
    """Measured: 10 workers took 2.90 s for 100 pages on this chip and 12
    took 3.05 s. os.cpu_count() reports logical processors, and half of
    those share an execution unit."""
    physical = mc._physical_core_count()
    logical = os.cpu_count() or 4
    assert 1 <= physical <= logical, \
        "physical (%s) / logical (%s) make no sense" % (physical, logical)
    # A tiny page so memory is not the binding constraint.
    n = mc._render_worker_count(1000, 100 * 100)
    assert n <= physical + physical // 2 + 1, \
        "%d workers on %d physical cores over-subscribes the machine" % (n, physical)
    assert n <= logical


def test_a_small_document_does_not_start_more_workers_than_it_has_pages():
    assert mc._render_worker_count(1, A4_1920) == 1
    assert mc._render_worker_count(3, A4_1920) <= 3


def test_the_pool_shrinks_when_the_pages_are_huge():
    """1.6 GB of peak working set is not survivable inside a 2.5 GB
    ceiling, and eight 3840 px pages in flight is exactly that."""
    small = mc._render_worker_count(500, A4_1920)
    large = mc._render_worker_count(500, A4_3840)
    assert large < small, (
        "4K pages get the same %d workers as 1920 px ones — the memory "
        "budget is not being applied" % large)
    per_worker = A4_3840 * mc._BYTES_PER_RENDERED_PIXEL
    assert large * per_worker <= mc._RENDER_MEMORY_BUDGET * 1.05, \
        "%d workers x %.0f MB exceeds the budget" % (large, per_worker / MB)


def test_a_normal_import_is_not_slowed_down_by_the_memory_budget():
    """The cap must only ever bite on a 4K board. At 1920 px a worker is
    about 46 MB, so the CPU shape is what should decide."""
    n = mc._render_worker_count(500, A4_1920)
    assert n * A4_1920 * mc._BYTES_PER_RENDERED_PIXEL < mc._RENDER_MEMORY_BUDGET, \
        "the budget is binding at 1920 px, where it should not be"
    assert n >= min(4, mc._physical_core_count()), \
        "only %d workers for an ordinary import" % n


def test_the_budget_leaves_room_for_the_rest_of_the_app():
    """Import peak + the pixmap cache + the app's own baseline all have to
    fit inside 2.5 GB."""
    ceiling = 2560 * MB
    baseline = 200 * MB          # measured ~75-120 MB with a board open
    total = mc._RENDER_MEMORY_BUDGET + default_budget_bytes() + baseline
    assert total <= ceiling, (
        "worst case is %.0f MB: %.0f import + %.0f cache + %.0f app"
        % (total / MB, mc._RENDER_MEMORY_BUDGET / MB,
           default_budget_bytes() / MB, baseline / MB))


# ── the encoder settings the measurements chose ─────────────────────────────

def test_the_webp_effort_stays_at_its_measured_optimum():
    """method=0 is 1.5x faster than method=2 for 18% more disk and no loss
    of fidelity, and the encode is 93% of an import."""
    assert mc._WEBP_METHOD == 0, (
        "WEBP method is %d; 0 was measured as 1.5x faster than 2 at the "
        "same fidelity" % mc._WEBP_METHOD)


def test_thumbnails_are_rendered_not_resized():
    """31 ms against 141 ms, for a byte-identical file."""
    import inspect
    src = inspect.getsource(mc._PageRenderer.render)
    assert "_save_thumb_from_page" in src, \
        "thumbnails are being resized from the full page again"


def test_there_is_still_a_fallback_when_the_page_cannot_be_re_rendered():
    """A missing thumbnail is a blank tile in the pages panel for the rest
    of the board's life."""
    import inspect
    src = inspect.getsource(mc._PageRenderer.render)
    assert "_save_thumb_from_image" in src, \
        "the thumbnail fallback is gone"


def test_the_page_buffer_is_not_copied_twice():
    """``pix.samples`` plus ``Image.frombytes`` is two full-page copies,
    neither of which releases the GIL."""
    import inspect
    src = inspect.getsource(mc._render_page_pixmap)
    assert "samples_mv" in src, "the GIL-held buffer copy is back"
    assert "frombuffer" in src


def test_the_save_path_does_no_per_page_filesystem_probing():
    """mkdir + exists + stat, twice a page, is six thousand GIL-holding
    syscalls for a thousand-page import."""
    import inspect
    for fn in (mc._save_webp, mc._save_thumb_from_page):
        # Code only. Both docstrings explain that they no longer do these
        # things, and matching the prose reports a failure for functions
        # that are correct — which is how this test failed the first time
        # it ran.
        src = inspect.getsource(fn).split('"""')[-1]
        assert "mkdir" not in src, \
            "%s creates its directory per call again" % fn.__name__
        assert ".stat()" not in src, \
            "%s stats the file it just wrote" % fn.__name__


# ── counting and estimating ─────────────────────────────────────────────────

def _tiny_pdf(pages, path):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((60, 90), "page %d" % (i + 1), fontsize=18)
    doc.save(str(path))
    doc.close()
    return path


def test_counting_pages_does_not_render_anything():
    tmp = Path(tempfile.mkdtemp())
    src = _tiny_pdf(12, tmp / "a.pdf")
    assert mc.count_pdf_pages(src) == 12
    assert not list(tmp.glob("*.webp")), "counting pages rendered something"


def test_counting_a_broken_file_is_zero_not_a_crash():
    tmp = Path(tempfile.mkdtemp())
    bad = tmp / "not.pdf"
    bad.write_bytes(b"this is not a pdf")
    assert mc.count_pdf_pages(bad) == 0
    assert mc.count_ppt_slides(bad) == 0
    assert mc.count_pdf_pages(tmp / "missing.pdf") == 0


def test_the_estimate_is_in_the_right_order_of_magnitude():
    """It is derived by timing a real page, so it should land close. The
    band is wide because this runs on whatever machine happens to be
    free — the point is that it is not off by 10x."""
    tmp = Path(tempfile.mkdtemp())
    src = _tiny_pdf(20, tmp / "b.pdf")
    est = mc.estimate_render_seconds(src, 1920)
    assert est is not None and est > 0
    import time
    out = Path(tempfile.mkdtemp())
    t0 = time.perf_counter()
    mc.render_pdf_pages(src, out, 1920)
    actual = time.perf_counter() - t0
    ratio = est / max(0.01, actual)
    assert 0.15 <= ratio <= 6.0, \
        "estimated %.2f s against an actual %.2f s (%.1fx out)" % (est, actual, ratio)


def test_the_estimate_scales_with_the_page_count():
    tmp = Path(tempfile.mkdtemp())
    src = _tiny_pdf(10, tmp / "c.pdf")
    small = mc.estimate_render_seconds(src, 1920, page_count=100)
    large = mc.estimate_render_seconds(src, 1920, page_count=1000)
    assert large > small * 5, \
        "10x the pages estimated at only %.1fx the time" % (large / small)


def test_the_estimate_is_none_rather_than_a_guess_when_it_cannot_tell():
    tmp = Path(tempfile.mkdtemp())
    bad = tmp / "nope.pdf"
    bad.write_bytes(b"garbage")
    assert mc.estimate_render_seconds(bad, 1920) is None


def test_durations_read_like_something_a_person_would_say():
    f = mc.format_duration
    assert f(5) == "under a minute"
    assert f(44) == "under a minute"
    assert "1 minute" in f(50)
    assert "5 minutes" in f(300)
    assert "hour" in f(3600)
    assert "2 hours" in f(7200)
    for s in (0, -5, 1e9):
        assert isinstance(f(s), str) and f(s)


# ── the warning ─────────────────────────────────────────────────────────────

def test_a_small_document_is_not_warned_about():
    from ui.util.import_progress import confirm_large_document
    tmp = Path(tempfile.mkdtemp())
    src = _tiny_pdf(5, tmp / "d.pdf")
    asked = []
    import ui.panels.aero_alert as aa
    original = aa.AeroAlert.question
    aa.AeroAlert.question = staticmethod(
        lambda *a, **k: asked.append(1) or aa.AeroAlert.Yes)
    try:
        assert confirm_large_document(None, src, kind="PDF", page_count=5)
        assert not asked, "a five-page PDF put a dialog up"
    finally:
        aa.AeroAlert.question = original


def test_a_large_document_is_warned_about_and_can_be_declined():
    from ui.util.import_progress import confirm_large_document
    tmp = Path(tempfile.mkdtemp())
    src = _tiny_pdf(5, tmp / "e.pdf")
    import ui.panels.aero_alert as aa
    original = aa.AeroAlert.question
    seen = {}

    def spy(parent, title, body, *a, **k):
        seen["title"] = title
        seen["body"] = body
        return aa.AeroAlert.No

    aa.AeroAlert.question = staticmethod(spy)
    try:
        went = confirm_large_document(None, src, kind="PDF",
                                      page_count=mc.LARGE_DOCUMENT_PAGES)
        assert went is False, "declining the warning still started the import"
        assert seen, "no warning was shown at the threshold"
        assert "{:,}".format(mc.LARGE_DOCUMENT_PAGES) in seen["body"], \
            "the warning does not say how many pages"
        assert src.name in seen["body"], "the warning does not name the file"
    finally:
        aa.AeroAlert.question = original


def test_the_threshold_is_where_it_was_asked_to_be():
    assert mc.LARGE_DOCUMENT_PAGES == 400


def test_both_import_paths_go_through_the_warning():
    import inspect
    from ui.util import import_progress as ip
    for fn in (ip.run_pdf_import_with_progress,
               ip.run_ppt_import_with_progress):
        src = inspect.getsource(fn)
        assert "confirm_large_document" in src, \
            "%s starts an import without checking the size" % fn.__name__


if __name__ == "__main__":
    import logging
    logging.disable(logging.ERROR)
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
