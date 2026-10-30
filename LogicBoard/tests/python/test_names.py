"""
test_names.py — the names of things: boards the teacher types, files that
arrive over the network.

Runs under pytest, and also standalone (``python tests/python/test_names.py``).

What is worth protecting here
-----------------------------
Two bugs, one shape: code that "sanitised" a name by throwing away the parts
it did not personally recognise.

**Board names.** ``_save_as_board`` ran ``re.sub(r"\\s+", "_", name)``, so a
teacher who typed "Class 11 Chapter 4" got a board called
"Class_11_Chapter_4" — in the list, in the title bar, forever, with no way to
get the spaces back. Nothing required it; a space is a legal Windows filename
character.

**Incoming files.** The receiver used ``werkzeug.utils.secure_filename``,
which is ASCII-only. ``गणित.pdf`` arrived on the board as a file called
``pdf`` — the name deleted AND the extension consumed, so nothing could
identify it as a PDF. In a school where Hindi filenames are ordinary, that is
not an edge case.

Both are now "change only what genuinely cannot be written". The tests below
are half "the name survives" and half "the dangerous thing still cannot
happen", because the second half is why the mangling was there to begin with.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from ui.util.lb_format import clean_board_name


# ── board names ─────────────────────────────────────────────────────────────

def test_spaces_survive():
    """THE regression. This is what a teacher actually types."""
    assert clean_board_name("Class 11 Chapter 4") == "Class 11 Chapter 4"
    assert clean_board_name("Lesson One") == "Lesson One"


def test_ordinary_punctuation_survives():
    for name in ("Chapter 4 - Thermodynamics",
                 "Physics (Revision)",
                 "Maths, Set 2",
                 "Test #3",
                 "50% Syllabus",
                 "Class 11-A & 11-B"):
        assert clean_board_name(name) == name, name


def test_characters_windows_refuses_are_replaced_not_kept():
    """These are the ones that genuinely cannot be written."""
    for bad in '<>:"/\\|?*':
        out = clean_board_name(f"Name{bad}Here")
        assert bad not in out, f"{bad!r} survived into {out!r}"


def test_a_colon_becomes_a_space_not_an_underscore():
    """"Ratio 3 4" reads like the name somebody meant; "Ratio_3_4" reads
    like a filename that escaped."""
    assert clean_board_name("Ratio 3:4") == "Ratio 3 4"


def test_runs_of_whitespace_collapse():
    assert clean_board_name("Physics    Notes") == "Physics Notes"
    assert clean_board_name("Tab\there") == "Tab here"


def test_the_ends_are_trimmed():
    """Leading and trailing spaces are invisible in a list, and Windows
    strips trailing ones anyway — so honouring them would mean the file on
    disk did not match the name on screen."""
    assert clean_board_name("   Padded   ") == "Padded"


def test_a_trailing_dot_goes():
    """Windows strips it silently, so "Chapter 4." would come back as
    "Chapter 4" — a board whose name no longer matches its record."""
    assert clean_board_name("Chapter 4.") == "Chapter 4"
    assert clean_board_name("Chapter 4...") == "Chapter 4"


def test_a_name_with_nothing_usable_still_saves_somewhere():
    for junk in ("", "   ", "***", "///", "..."):
        assert clean_board_name(junk) == "Board", junk


def test_a_very_long_name_is_capped():
    out = clean_board_name("a" * 400)
    assert 0 < len(out) <= 120


def test_control_characters_never_reach_the_filesystem():
    out = clean_board_name("Bad\x00Name\x07Here")
    assert "\x00" not in out and "\x07" not in out
    assert "Bad" in out and "Here" in out


def test_a_cleaned_name_is_stable():
    """Cleaning an already-clean name must not keep changing it — the board
    gets re-saved under its own name constantly."""
    for name in ("Class 11 Chapter 4", "Ratio 3 4", "Board"):
        assert clean_board_name(clean_board_name(name)) == clean_board_name(name)


def test_the_panel_does_not_sanitise_before_the_window_does():
    """Two sanitisers with different rules ran in series, so the name was
    mangled twice. The panel now hands the raw text over."""
    import inspect
    from ui.panels import file_panel
    src = inspect.getsource(file_panel)
    assert "_sanitize_name" not in src, \
        "the file panel has its own name mangler again"


# ── incoming filenames ──────────────────────────────────────────────────────

def _incoming(name):
    from ui.logicshare.service import safe_incoming_filename
    return safe_incoming_filename(name)


def test_a_hindi_filename_arrives_intact():
    """THE regression. secure_filename turned this into "pdf"."""
    assert _incoming("गणित.pdf") == "गणित.pdf"
    assert _incoming("नोट्स.png") == "नोट्स.png"
    assert _incoming("भौतिक विज्ञान.pptx") == "भौतिक विज्ञान.pptx"


def test_a_non_ascii_name_keeps_its_extension():
    """Losing the extension is the worse half: the board identifies file
    types by suffix, so it could not preview or open what arrived."""
    for name in ("गणित.pdf", "Ωmega.pdf", "résumé.pdf"):
        assert _incoming(name).endswith(".pdf"), name


def test_accents_and_dashes_survive():
    assert _incoming("résumé.pdf") == "résumé.pdf"
    assert _incoming("Notes—Chapter4.pdf") == "Notes—Chapter4.pdf"


def test_spaces_survive_in_incoming_names_too():
    assert _incoming("Class 11 Notes.pdf") == "Class 11 Notes.pdf"


def test_a_path_cannot_escape_the_shared_folder():
    """The reason a sanitiser was there at all. Both separators, because the
    sender may be on either platform."""
    assert _incoming("../../etc/passwd") == "passwd"
    assert _incoming("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert _incoming("/absolute/path/file.pdf") == "file.pdf"
    assert _incoming("C:\\Windows\\evil.dll") == "evil.dll"


def test_no_separator_ever_survives():
    for name in ("a/b.pdf", "a\\b.pdf", "../x", "..\\x"):
        out = _incoming(name)
        assert "/" not in out and "\\" not in out, f"{name!r} -> {out!r}"


def test_a_name_that_is_only_dots_is_refused():
    """"." and ".." name a directory, not a file."""
    for name in ("..", ".", "..."):
        assert _incoming(name) == "upload.bin", name


def test_an_empty_or_missing_name_gets_a_fallback():
    assert _incoming("") == "upload.bin"
    assert _incoming(None) == "upload.bin"


def test_characters_windows_refuses_are_replaced_in_incoming_names():
    out = _incoming('bad:name?.pdf')
    assert ":" not in out and "?" not in out
    assert out.endswith(".pdf")


def test_a_very_long_incoming_name_keeps_its_extension():
    """Truncating past the dot is how a PDF stops being a PDF."""
    out = _incoming("a" * 400 + ".pdf")
    assert len(out) <= 180
    assert out.endswith(".pdf"), out


def test_trailing_dots_and_spaces_go():
    """Windows strips them, so the file on disk would not match the name we
    told the teacher we saved."""
    assert _incoming("trailing.  ") == "trailing"
    assert _incoming("notes.pdf ") == "notes.pdf"


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
