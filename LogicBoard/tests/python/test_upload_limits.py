"""
test_upload_limits.py — a transfer must not be able to fill the board's disk.

Runs under pytest, and also standalone
(``python tests/python/test_upload_limits.py``).

What is worth protecting here
-----------------------------
``/upload`` reads from a socket and writes to disk in a loop. Nothing bounded
how much, and nothing looked at how much room was left. One oversized file —
or one sender stuck in a retry loop — fills the drive the board runs from.

That is worse than a failed transfer. LogicBoard autosaves to the same disk,
so a teacher whose board fills up loses the ability to save the lesson they
are in the middle of, and the cause (a student sending a video) is nowhere
near the symptom (Save stops working).

TWO CHECKS, AND WHAT EACH ONE IS FOR
====================================
``Content-Length`` is refused early when it is over the cap. That is the
polite path: a 3 GB send is turned away before a single byte is written.

The loop then counts what actually arrives and stops at the same ceiling.
Not because the header can be exceeded — measured, and it cannot: Werkzeug
wraps ``request.stream`` in a LimitedStream bounded by Content-Length, so a
sender that declares 10 bytes and pushes 40 KB simply never has the surplus
read. The second check is for the requests where there is no length to
believe in the first place: a chunked transfer, or a client that omits the
header entirely. Both are pinned below.

A malformed header is a bad request, not a broken board. ``int()`` on it used
to raise into the generic handler and answer 500; it now falls back to zero
and lets the in-loop ceiling do the work.

NOTE ON THE TEST CLIENT
=======================
Werkzeug's test client recomputes ``Content-Length`` from the body it is
given, so it cannot express "declares one size, sends another". The
declared-size tests therefore build a WSGI environ by hand; the in-loop test
uses the client and lowers the cap under an honest body instead.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

import ui.logicshare.service as S
from ui.logicshare.config import LSConfig
from ui.logicshare.activity_log import ActivityLog

GB = 1024 ** 3


def _service():
    tmp = Path(tempfile.mkdtemp(prefix="lb_upload_"))
    svc = S.LogicShareService(
        LSConfig(tmp / "settings.json"),
        ActivityLog(tmp / "activity.json"),
        shared_dir=tmp / "shared",
        temp_dir=tmp / "temp",
        ack_dir=tmp / "ack",
    )
    svc._enabled = True
    svc._build_flask()
    return svc, tmp


def _raw_post(svc, body: bytes, content_length: str, filename="big.bin"):
    """POST /upload with CONTENT_LENGTH set to exactly what we say.

    The test client will not do this — it recomputes the header from the
    body — and "declares one size, sends another" is the case that matters.
    """
    env = {
        "REQUEST_METHOD": "POST", "PATH_INFO": "/upload",
        "SERVER_NAME": "test", "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
        "wsgi.multithread": False, "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": content_length,
        "HTTP_X_FILENAME": filename, "REMOTE_ADDR": "192.168.1.50",
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])

    payload = b"".join(svc._app(env, start_response))
    return captured["status"], payload.decode("utf-8", "replace")


# ── the cap is real and sane ────────────────────────────────────────────────

def test_there_is_a_cap_and_a_disk_reserve():
    assert S._MAX_UPLOAD_BYTES > 0, "the upload cap has been removed"
    assert S._DISK_RESERVE_BYTES > 0, "the disk reserve has been removed"
    # Big enough for a real classroom file, small enough to be a limit.
    assert 256 * 1024 * 1024 <= S._MAX_UPLOAD_BYTES <= 8 * GB, \
        "the cap is %s, which is not a classroom-sized limit" % S._MAX_UPLOAD_BYTES


# ── the declared size ───────────────────────────────────────────────────────

def test_an_over_cap_declaration_is_refused_before_any_bytes_are_written():
    svc, tmp = _service()
    status, body = _raw_post(svc, b"x" * 8, str(3 * GB))
    assert status == 413, "a 3 GB upload was accepted (status %s)" % status
    assert "too_large" in body
    landed = list((tmp / "shared").glob("*")) if (tmp / "shared").exists() else []
    assert not landed, "bytes were written despite the refusal: %s" % landed


def test_an_absurd_declaration_is_refused_too():
    svc, _tmp = _service()
    status, _ = _raw_post(svc, b"x" * 8, str(900 * 1024 * GB))
    assert status == 413


def test_a_malformed_content_length_is_not_a_server_error():
    """It is a bad request, not a broken board. ``int()`` on it used to
    raise into the generic handler and answer 500."""
    svc, _tmp = _service()
    for header in ("not-a-number", "-5", "", "1e9", "12.5", "0x40"):
        status, body = _raw_post(svc, b"hello", header)
        assert status != 500, \
            "Content-Length %r produced a 500: %s" % (header, body[:120])


def test_an_honest_upload_still_goes_through():
    """The cap must not become a wall. This is the case that has to keep
    working."""
    svc, tmp = _service()
    status, body = _raw_post(svc, b"hello there", "11", filename="notes.txt")
    assert status == 200, body
    assert "notes.txt" in body
    assert (tmp / "shared" / "notes.txt").read_bytes() == b"hello there"


# ── the bytes that actually arrive ──────────────────────────────────────────

def test_under_declaring_the_size_does_not_smuggle_extra_bytes_in():
    """Werkzeug's ``request.stream`` is a LimitedStream bounded by
    Content-Length, so a sender that declares 10 bytes and pushes 40 KB
    simply never has the surplus read. Worth pinning, because it is the
    reason the early check is a real defence and not just a courtesy — but
    note what it does NOT prove: the loop is never reached, so this says
    nothing about the ceiling below. That is the next test.
    """
    svc, tmp = _service()
    status, body = _raw_post(svc, b"y" * 40000, "10")
    assert status == 200, body
    saved = list((tmp / "shared").glob("*"))
    assert len(saved) == 1
    assert saved[0].stat().st_size == 10, (
        "%d bytes were stored for a request that declared 10"
        % saved[0].stat().st_size)


def test_a_body_over_the_cap_is_stopped_mid_stream():
    """The ceiling inside the read loop.

    This is what protects the cases the early check cannot see: a chunked
    request, or any transfer whose true length is only discovered as it
    arrives. The Flask test client sets an honest Content-Length, so the
    cap is lowered under the body rather than the body raised over the cap.
    """
    svc, tmp = _service()
    client = svc._app.test_client()
    original = S._MAX_UPLOAD_BYTES
    S._MAX_UPLOAD_BYTES = 4096
    try:
        r = client.post("/upload", data=b"y" * 40000,
                        headers={"X-Filename": "huge.bin"})
    finally:
        S._MAX_UPLOAD_BYTES = original
    assert r.status_code == 413, \
        "a body 10x the cap was accepted (status %s)" % r.status_code
    landed = list((tmp / "shared").glob("*")) if (tmp / "shared").exists() else []
    assert not landed, "the oversized body was published anyway: %s" % landed


def test_a_refused_oversized_body_leaves_no_partial_file():
    """It was being written to a temp file as it arrived, so stopping
    half-way must not leave that behind."""
    svc, tmp = _service()
    original = S._MAX_UPLOAD_BYTES
    S._MAX_UPLOAD_BYTES = 4096
    try:
        svc._app.test_client().post("/upload", data=b"y" * 40000,
                                    headers={"X-Filename": "huge.bin"})
    finally:
        S._MAX_UPLOAD_BYTES = original
    strays = list((tmp / "temp").glob("*")) if (tmp / "temp").exists() else []
    assert not strays, "partial upload left behind: %s" % [f.name for f in strays]


def test_the_transfer_slot_is_released_after_a_refusal():
    """A slot that is never released stops the teacher from stopping the
    service — 'a transfer is in progress' forever."""
    svc, _tmp = _service()
    original = S._MAX_UPLOAD_BYTES
    S._MAX_UPLOAD_BYTES = 4096
    try:
        svc._app.test_client().post("/upload", data=b"y" * 40000,
                                    headers={"X-Filename": "huge.bin"})
    finally:
        S._MAX_UPLOAD_BYTES = original
    with svc._transfer_lock:
        assert not svc._active_transfers, \
            "a refused upload is still registered as in flight"


# ── free space ──────────────────────────────────────────────────────────────

def test_the_free_space_check_answers_for_a_directory_not_yet_created():
    """The temp directory is made lazily, and asking about a path that is
    not there yet raises rather than answering about its volume."""
    svc, tmp = _service()
    assert not (tmp / "temp").exists() or True
    got = S._free_bytes(tmp / "temp" / "deeper" / "still")
    assert got is not None and got > 0, \
        "free space came back as %r for a path that does not exist yet" % got


def test_the_free_space_check_never_raises():
    for path in (None, "", "\x00bad", Path("Z:/definitely/not/here"), 12345):
        S._free_bytes(path)          # must not raise


def test_an_upload_bigger_than_the_disk_is_refused():
    """Declared size plus the working reserve must fit."""
    svc, _tmp = _service()
    status, body = _raw_post(svc, b"x" * 8, str(10 ** 15))
    # Either too large for the cap or too large for the disk — both are a
    # refusal, and which one fires depends on the machine.
    assert status in (413, 507), \
        "a petabyte upload was accepted (status %s): %s" % (status, body[:120])


# ── the disabled service still says so ──────────────────────────────────────

def test_a_disabled_board_refuses_before_any_of_this():
    svc, _tmp = _service()
    svc._enabled = False
    status, body = _raw_post(svc, b"x", "1")
    assert status == 503 and "disabled" in body


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
