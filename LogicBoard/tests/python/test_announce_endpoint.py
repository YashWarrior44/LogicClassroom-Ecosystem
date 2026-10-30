"""
test_announce_endpoint.py — /announce takes its body from another machine.

Runs under pytest, and also standalone
(``python tests/python/test_announce_endpoint.py``).

What is worth protecting here
-----------------------------
``/announce`` is how a teacher device puts a popup on a classroom wall. The
body is JSON off the network, so its SHAPE is not guaranteed — not because
anyone is attacking a school LAN, but because a field can be a number when
the sender's form left it as one, or a client can be a version older than the
board.

THREE 500s, FOUND BY SENDING THE WRONG SHAPES
=============================================
  * a JSON **list** body — ``request.get_json(...) or {}`` leaves a list in
    place, because a non-empty list is truthy, and ``data.get`` then raises
    AttributeError;
  * ``title`` as a **number** — ``(data.get("title") or "").strip()`` looks
    defensive and is not: the ``or`` only substitutes when the value is
    FALSY, so ``123`` sails past it and ``.strip()`` fails on an int;
  * ``title`` as a **dict** — the same line, the same way.

All three answered 500 with the exception text echoed back.

AND NOTHING WAS BOUNDED
=======================
No length limit on any field. A 200 KB title went to the UI as it arrived,
to be drawn on a classroom wall. Control characters went too — a NUL in a
title truncates the string in some Win32 text APIs, which turns "Assembly at
10" into "Assembly" on the one screen that matters.
"""
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

BASE = {"announcement_id": "a1", "teacher_device_id": "t1", "ack_port": 16385}


def _service():
    tmp = Path(tempfile.mkdtemp(prefix="lb_announce_"))
    svc = S.LogicShareService(
        LSConfig(tmp / "settings.json"),
        ActivityLog(tmp / "activity.json"),
        shared_dir=tmp / "shared", temp_dir=tmp / "temp", ack_dir=tmp / "ack",
    )
    svc._enabled = True
    svc._build_flask()
    delivered = []
    svc.announcement_received.connect(delivered.append)
    return svc, svc._app.test_client(), delivered


def _post(client, payload=None, raw=None):
    if raw is not None:
        return client.post("/announce", data=raw,
                           content_type="application/json")
    return client.post("/announce", json=payload)


def _ann(**over):
    d = dict(BASE)
    d.setdefault("title", "Assembly")
    d.setdefault("message", "Hall at 10")
    d.update(over)
    return d


# ── the shapes that used to be 500s ─────────────────────────────────────────

def test_a_body_that_is_not_an_object_is_a_bad_request_not_a_crash():
    """THE first regression. A JSON list is truthy, so ``or {}`` left it in
    place and ``data.get`` raised."""
    _svc, client, _seen = _service()
    for raw in (b"[1,2,3]", b'"hello"', b"42", b"true", b"[]", b"", b"<<<junk"):
        r = _post(client, raw=raw)
        assert r.status_code != 500, \
            "body %r produced a 500: %s" % (raw[:20], r.get_data(as_text=True)[:120])
        assert r.status_code == 400, "body %r gave %s" % (raw[:20], r.status_code)


def test_a_field_of_the_wrong_type_is_never_a_crash():
    """THE second and third. ``(x or "").strip()`` only guards the falsy
    case, so a number or a dict went straight into ``.strip()``."""
    _svc, client, _seen = _service()
    hostile = [123, 3.5, {"a": 1}, [1, 2], True, False, None, 0, -1, 1e309]
    for i, value in enumerate(hostile):
        for field in ("title", "message", "short_name", "first_name",
                      "surname", "title_honorific", "announcement_id",
                      "teacher_device_id"):
            r = _post(client, _ann(**{field: value,
                                      "announcement_id": "id%d%s" % (i, field)}))
            assert r.status_code != 500, (
                "%s=%r produced a 500: %s"
                % (field, value, r.get_data(as_text=True)[:120]))


def test_a_nonsense_ack_port_does_not_crash():
    _svc, client, _seen = _service()
    for port in ("abc", -1, 10 ** 9, None, {"a": 1}, [8080]):
        r = _post(client, _ann(ack_port=port, announcement_id="p%s" % port))
        assert r.status_code != 500, "ack_port=%r produced a 500" % (port,)


# ── nothing unbounded reaches the wall ──────────────────────────────────────

def test_every_field_is_bounded_before_it_reaches_the_ui():
    _svc, client, seen = _service()
    big = "A" * 200000
    _post(client, _ann(title=big, announcement_id="big1"))
    _post(client, _ann(message=big, announcement_id="big2"))
    assert seen, "nothing was delivered — the fixture is not exercising the path"
    for info in seen:
        assert len(info["title"]) <= S._ANNOUNCE_LIMITS["title"], \
            "a %d-character title reached the UI" % len(info["title"])
        assert len(info["message"]) <= S._ANNOUNCE_LIMITS["message"], \
            "a %d-character message reached the UI" % len(info["message"])


def test_the_limits_are_sane_for_a_real_announcement():
    """Bounded is not the same as useful. A teacher must still be able to
    write a normal notice."""
    lim = S._ANNOUNCE_LIMITS
    assert lim["title"] >= 80, "the title limit is too short for a headline"
    assert lim["message"] >= 500, "the message limit cannot hold a notice"
    _svc, client, seen = _service()
    title = "Assembly moved to the main hall"
    message = ("Please bring your students to the main hall at 10:00 instead "
               "of the auditorium. The projector in the auditorium is being "
               "serviced this morning. " * 4)
    _post(client, _ann(title=title, message=message))
    assert seen[-1]["title"] == title
    assert seen[-1]["message"] == message.strip()


def test_control_characters_are_stripped():
    """A NUL in a title truncates the string in some Win32 text APIs, which
    turns "Assembly at 10" into "Assembly" on the one screen that matters."""
    _svc, client, seen = _service()
    _post(client, _ann(title="Assembly\x00 at 10",
                       message="Hall\x07\x1b[31m now",
                       short_name="y\x00z"))
    info = seen[-1]
    for field in ("title", "message", "short_name"):
        assert "\x00" not in info[field], "a NUL survived in %s" % field
        assert "\x07" not in info[field] and "\x1b" not in info[field], \
            "a control character survived in %s" % field
    assert "Assembly" in info["title"] and "10" in info["title"], \
        "stripping the NUL took the rest of the title with it"


def test_a_structure_where_a_string_belongs_is_treated_as_absent():
    """There is nothing sensible to draw for ``{"a": 1}``, so it must not
    become the literal text of a dict on a classroom wall."""
    _svc, client, seen = _service()
    r = _post(client, _ann(title={"a": 1}))
    assert r.status_code == 400, "a dict title was accepted as a title"
    r = _post(client, _ann(title="Fine", message=[1, 2, 3]))
    assert r.status_code == 400, "a list message was accepted"
    for info in seen:
        assert "{" not in info["title"] and "[" not in info["title"]


# ── the behaviour that has to keep working ──────────────────────────────────

def test_a_normal_announcement_still_gets_through():
    _svc, client, seen = _service()
    r = _post(client, _ann(first_name="Yash", surname="Jain",
                           short_name="yj", title_honorific="Sir"))
    assert r.status_code == 200, r.get_data(as_text=True)
    info = seen[-1]
    assert info["title"] == "Assembly"
    assert info["message"] == "Hall at 10"
    assert info["short_name"] == "YJ", "short_name is no longer upper-cased"
    assert info["ack_expected"] is True


def test_a_missing_title_or_message_is_still_refused():
    _svc, client, _seen = _service()
    assert _post(client, {}).status_code == 400
    assert _post(client, _ann(title="")).status_code == 400
    assert _post(client, _ann(message="   ")).status_code == 400


def test_a_repeated_announcement_id_does_not_put_up_a_second_popup():
    """A teacher retrying a send it never got a response to is NORMAL. The
    coercion must not have broken the de-duplication that depends on the id
    surviving intact."""
    _svc, client, seen = _service()
    for _ in range(4):
        assert _post(client, _ann(announcement_id="same-id")).status_code == 200
    assert len(seen) == 1, "%d popups for one announcement id" % len(seen)


def test_a_legacy_sender_with_no_ids_is_still_accepted():
    """Older LogicShare clients send no announcement_id — they get an
    announcement nobody can acknowledge, which is the old behaviour and not
    an error."""
    _svc, client, seen = _service()
    r = _post(client, {"title": "Fire drill", "message": "11:00, field"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen[-1]["ack_expected"] is False
    assert seen[-1]["id"], "a legacy announcement got no local id"


def test_a_disabled_board_refuses_announcements():
    svc, client, _seen = _service()
    svc._enabled = False
    assert _post(client, _ann()).status_code == 503


# ── the helper itself ───────────────────────────────────────────────────────

def test_clean_field_never_raises_and_never_exceeds_its_limit():
    weird = [None, 0, 1, -1, True, False, 1.5, float("nan"), float("inf"),
             "", "  ", "x" * 10000, {"a": 1}, [1], (1,), set(), object(),
             b"bytes", "\x00\x01\x02", "​‮"]
    for value in weird:
        out = S._clean_field(value, 50)
        assert isinstance(out, str), "%r gave %r" % (value, out)
        assert len(out) <= 50, "%r gave %d characters" % (value, len(out))


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
