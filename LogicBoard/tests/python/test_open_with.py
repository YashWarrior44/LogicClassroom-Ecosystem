"""
test_open_with.py — PDFs and decks open WITH LogicBoard.

Runs under pytest, and also standalone
(``python tests/python/test_open_with.py``).

The chain, end to end:

  Explorer runs  "LogicBoard.exe" "<file>"     (file_association registers it)
    → app._parse_cli finds the file            (or a 2nd launch forwards it
                                                over the IPC pipe as "open")
    → ApplicationCore.open_file queues it until the window exists
    → MainWindow.open_external_file asks "new board or saved board?"
    → the pages arrive, one board page per PDF page / slide, fitted.

Nothing here writes the real registry: the registration is driven against a
recording stand-in for ``winreg``.
"""
import os
import re
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from ui.util import file_association as fa


# ── the icons ───────────────────────────────────────────────────────────────

def test_the_pdf_and_ppt_icons_are_real_multi_frame_icos():
    from PIL import Image
    for name in ("pdf.ico", "ppt.ico"):
        p = ROOT / "assets" / "icons" / name
        assert p.is_file(), "%s is missing — run scripts/build_filetype_icons.py" % name
        sizes = Image.open(p).ico.sizes()
        for s in (16, 32, 48, 256):
            assert (s, s) in sizes, "%s has no %dpx frame" % (name, s)


def test_the_app_icon_is_still_the_logo():
    """pdf.ico now sits beside logo.ico, and the lookup used to be "first
    .ico in alphabetical order"."""
    from ui.util.paths import find_app_icon_path
    assert find_app_icon_path().name == "logo.ico"


# ── registration ────────────────────────────────────────────────────────────

class _Reg:
    """Records every key and value written, like a tiny registry."""
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ, REG_NONE, REG_DWORD = 1, 0, 4

    def __init__(self):
        self.values = {}

    def CreateKey(self, root, sub):                     # noqa: N802
        reg = self

        class K:
            def __enter__(s):
                reg.values.setdefault(sub, {})
                return sub

            def __exit__(s, *a):
                return False
        return K()

    def SetValueEx(self, key, name, _r, _t, value):     # noqa: N802
        self.values.setdefault(key, {})[name] = value

    def OpenKey(self, root, sub, *_a):                  # noqa: N802
        if sub not in self.values:
            raise FileNotFoundError(sub)

        class K:
            def __enter__(s):
                return sub

            def __exit__(s, *a):
                return False
        return K()

    def CloseKey(self, _k):                              # noqa: N802
        pass

    def QueryValueEx(self, key, name):                   # noqa: N802
        try:
            return self.values[key][name], 1
        except KeyError:
            raise FileNotFoundError(name)

    def DeleteKey(self, root, sub):                      # noqa: N802
        if sub not in self.values:
            raise FileNotFoundError(sub)
        del self.values[sub]


def _register(tmp: Path, installed: bool, reg=None):
    exe = tmp / "LogicBoard.exe"
    exe.write_bytes(b"MZ")
    if installed:
        (tmp / "Uninstaller.exe").write_bytes(b"MZ")
    icons = tmp / "icons"
    icons.mkdir(exist_ok=True)
    for n in ("logo.ico", "pdf.ico", "ppt.ico"):
        (icons / n).write_bytes(b"\0")
    reg = reg if reg is not None else _Reg()
    saved = (getattr(sys, "frozen", None), sys.executable,
             sys.modules.get("winreg"), fa._refresh_shell)
    try:
        sys.frozen = True
        sys.executable = str(exe)
        sys.modules["winreg"] = reg
        fa._refresh_shell = lambda: None
        ok = fa.register_file_association(icons / "logo.ico", tmp)
    finally:
        frozen, sys.executable, wr, fa._refresh_shell = saved
        if frozen is None:
            del sys.frozen
        if wr is not None:
            sys.modules["winreg"] = wr
    return ok, reg.values, exe


def test_an_installed_copy_offers_itself_for_pdf_and_powerpoint():
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as d:
        ok, v, exe = _register(Path(d), installed=True)
        assert ok
        cmd = f'"{exe.resolve()}" "%1"'
        for ext, (pid, _name, ico) in fa.DOCUMENT_TYPES.items():
            assert pid in v[rf"Software\Classes\{ext}\OpenWithProgids"], \
                "LogicBoard is not in Open with for %s" % ext
            assert v[rf"Software\Classes\{pid}\shell\open\command"][""] == cmd
            icon = v[rf"Software\Classes\{pid}\DefaultIcon"][""]
            assert icon.endswith(f'{ico}",0'), \
                "%s files would not get LogicBoard's %s: %s" % (ext, ico, icon)
            assert v[fa.CAPABILITIES_KEY + r"\FileAssociations"][ext] == pid
        assert v[r"Software\RegisteredApplications"][fa.REGISTERED_APP_NAME] \
            == fa.CAPABILITIES_KEY, "LogicBoard is missing from Default apps"


def test_the_users_chosen_pdf_app_is_never_overwritten():
    """Offered, never taken: the extension key's own default value belongs
    to whichever app the user picked. Writing it is the takeover Windows'
    UserChoice hash exists to stop."""
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as d:
        _ok, v, _exe = _register(Path(d), installed=True)
        for ext in fa.DOCUMENT_TYPES:
            assert "" not in v.get(rf"Software\Classes\{ext}", {}), \
                "the default app for %s was overwritten" % ext


def test_a_copy_that_was_not_installed_registers_nothing():
    """The source tree / a dist folder used to repoint .lb at itself."""
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as d:
        ok, v, _exe = _register(Path(d), installed=False)
        assert not ok and not v, "a non-installed copy wrote %s" % list(v)[:3]


def test_the_uninstaller_removes_exactly_what_the_app_registers():
    src = (ROOT / "LogicBoardInstaller" / "LogicBoardInstaller.cpp").read_text(
        encoding="utf-8", errors="replace")
    i = src.index("// File associations.")
    block = src[i:src.index("FirewallDropRulesUnder(installDir);", i)]
    for pid in fa.ALL_PROGIDS:
        assert f'L"{pid}"' in block, "the uninstaller leaves %s behind" % pid
    for ext in fa.DOCUMENT_TYPES:
        assert f'L"{ext}"' in block, \
            "the uninstaller leaves LogicBoard in Open with for %s" % ext
    assert "RegisteredApplications" in block and "Capabilities" in block


# ── the launch ──────────────────────────────────────────────────────────────

def test_the_file_explorer_passes_is_found_on_the_command_line():
    from ui.app import _parse_cli
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "Chapter 4 📜.pdf"
        f.write_bytes(b"%PDF")
        got = _parse_cli(["LogicBoard.exe", str(f)])
        assert Path(got["open_file"]) == f.resolve()
        # Flags and a flag's value are never mistaken for the file.
        got = _parse_cli(["LogicBoard.exe", "--background"])
        assert got["open_file"] is None
        got = _parse_cli(["LogicBoard.exe", "--grant-firewall-access",
                          str(f)])
        assert got["open_file"] is None
        assert got["grant_firewall_rule"] == str(f)


def test_a_second_launch_forwards_the_path_intact():
    """Through the pipe as pure ASCII — the emoji in this project's own
    path is exactly what would break a naive encoder."""
    from ui.util.single_instance import _decode, _decode_path, _encode
    path = r"C:\Users\teacher\Source Code📜\Optics — Ch 4.pdf"
    raw = _encode("open", "3.1.2", path)
    raw.decode("ascii")          # raises if anything non-ASCII got through
    cmd, ver = _decode(raw.decode("ascii"))
    assert (cmd, ver) == ("open", "3.1.2")
    assert _decode_path(raw.decode("ascii")) == path
    # The old messages still read the same.
    assert _encode("show", None) == b"show\n"
    assert _decode_path("show") is None


def test_a_file_waits_for_the_window_then_opens():
    """Opened while LogicBoard sat in the background: the window is built
    first (after a splash), and the file must not be dropped meanwhile."""
    from ui.application_core import ApplicationCore
    core = ApplicationCore.__new__(ApplicationCore)
    opened = []
    core._pending_files = []
    core._opening_file = False
    core._main_window = None
    core._building_window = True
    core.show_ui = lambda: None
    core._raise_existing_window = lambda: None
    core.open_file("a.pdf")
    core.open_file("b.pptx")
    _app.processEvents()
    assert core._pending_files == ["a.pdf", "b.pptx"], "a file was lost"
    core._main_window = types.SimpleNamespace(
        open_external_file=lambda p: opened.append(p.name))
    core._building_window = False
    core._drain_pending_files()
    for _ in range(5):
        _app.processEvents()
    assert opened == ["a.pdf", "b.pptx"], opened


# ── the import ──────────────────────────────────────────────────────────────

def _pdf(path: Path, pages: int) -> Path:
    import fitz
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    doc.save(str(path))
    return path


def _window(tmp: Path):
    import ui.util.paths as paths
    paths.saves_dir = lambda: tmp
    from ui.main_window import MainWindow
    w = MainWindow()
    w._saves_dir = tmp
    return w


def _answer(choice):
    import ui.panels.open_into_dialog as oid
    oid.OpenIntoDialog.ask = classmethod(lambda cls, *a, **k: choice)
    return oid


def test_a_pdf_opens_as_pages_on_a_new_board():
    try:
        import fitz   # noqa: F401
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pdf = _pdf(tmp / "Optics.pdf", 5)
        w = _window(tmp)
        oid = _answer(None)
        _answer(oid.NEW_BOARD)
        w.open_external_file(pdf)
        c = w._canvas
        assert c.page_count == 5, "5 PDF pages became %d board pages" % c.page_count
        assert all(c.scene_at(i).has_bg_image for i in range(5))
        assert c.current_page == 0
        assert set(c.bg_fit_map().values()) == {"fit"}, \
            "pages were not fitted to the canvas: %s" % c.bg_fit_map()


def test_a_pdf_is_added_after_a_saved_boards_last_page():
    try:
        import fitz   # noqa: F401
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        pdf = _pdf(tmp / "Optics.pdf", 3)
        w = _window(tmp)
        oid = _answer(None)
        c = w._canvas
        # A two-page board with a picture already on page 2.
        _answer(oid.NEW_BOARD)
        w.open_external_file(_pdf(tmp / "old.pdf", 2))
        old = [c.scene_at(i).bg_image for i in range(2)]
        w._do_save(tmp / "Physics.lb", silent=True)
        w._new_board()
        assert w._current_lb_path is None

        _answer(tmp / "Physics.lb")
        w.open_external_file(pdf)
        assert w._current_lb_path.name == "Physics.lb", \
            "the saved board was not the one opened"
        assert c.page_count == 5, "expected 2 old + 3 new, got %d" % c.page_count
        assert c.current_page == 2, "should land on the first new page"
        assert [c.scene_at(i).bg_image for i in range(2)] == old, \
            "the board's own pages were overwritten"
        assert all(c.scene_at(i).has_bg_image for i in range(2, 5))


def test_cancel_changes_nothing():
    try:
        import fitz   # noqa: F401
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        w = _window(tmp)
        _answer(None)
        w.open_external_file(_pdf(tmp / "x.pdf", 2))
        assert w._canvas.page_count == 1
        assert not w._canvas.scene_at(0).has_bg_image


def test_the_picker_says_what_the_button_will_do():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        boards = []
        for n in ("Physics", "Chemistry"):
            p = tmp / f"{n}.lb"
            p.write_bytes(b"x")
            boards.append(p)
        f = tmp / "deck.pptx"
        f.write_bytes(b"PK")
        from ui.panels.open_into_dialog import NEW_BOARD, OpenIntoDialog
        dlg = OpenIntoDialog(f, None, boards=boards)
        assert dlg._selected == NEW_BOARD, "a new board must be the default"
        assert dlg._go_btn.text() == "Open in new board"
        dlg._pick(boards[1])
        assert dlg._go_btn.text() == "Add to Chemistry"
        dlg._go()
        assert dlg.choice() == boards[1]



# ── PDF / PowerPoint offered ONCE; .lb always LogicBoard's ──────────────────

def test_pdf_and_powerpoint_are_offered_once_and_never_put_back():
    """A teacher who removes LogicBoard from "Open with" for PDFs is not
    overruled on the next launch."""
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        ok, v, _exe = _register(d, installed=True)
        assert ok and v[fa.DOCS_ONCE_KEY][fa.DOCS_ONCE_VALUE] == 1, \
            "the one-time registration was not recorded"
        reg = _Reg()
        reg.values = {k: dict(x) for k, x in v.items()}
        # The teacher takes LogicBoard out of Open with for PDF, and the
        # PowerPoint ProgID is gone altogether.
        del reg.values[r"Software\Classes\.pdf\OpenWithProgids"]["LogicBoard.PDF"]
        for k in [k for k in reg.values if k.startswith(r"Software\Classes\LogicBoard.PPTX")]:
            del reg.values[k]
        # A launch from a NEW place (an update moved the exe): the marker in
        # the saves folder no longer matches, so registration runs again.
        d2 = d / "moved"
        d2.mkdir()
        ok, v2, exe2 = _register(d2, installed=True, reg=reg)
        assert ok
        assert "LogicBoard.PDF" not in v2[r"Software\Classes\.pdf\OpenWithProgids"], \
            "LogicBoard put itself back into Open with for PDF"
        assert r"Software\Classes\LogicBoard.PPTX" not in v2, \
            "a removed PowerPoint entry was recreated"
        # What still exists is REPAIRED, so it never points at the old exe.
        cmd = v2[r"Software\Classes\LogicBoard.PDF\shell\open\command"][""]
        assert str(exe2.resolve()) in cmd, cmd
        # And .lb, LogicBoard's own type, is always current.
        assert str(exe2.resolve()) in \
            v2[r"Software\Classes\LogicBoard.Board.v3\shell\open\command"][""]


def _with_fake_winreg(reg, fn):
    saved = (sys.modules.get("winreg"), fa._refresh_shell)
    sys.modules["winreg"] = reg
    fa._refresh_shell = lambda: None
    try:
        return fn()
    finally:
        if saved[0] is not None:
            sys.modules["winreg"] = saved[0]
        fa._refresh_shell = saved[1]


def test_lb_is_handed_back_when_another_app_was_chosen():
    if sys.platform != "win32":
        return
    base = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            r"\.lb\UserChoice")
    reg = _Reg()
    reg.values = {base: {"ProgId": "Applications\\notepad.exe", "Hash": "x"},
                  base + "Latest": {"Hash": "y"},
                  base + "Latest\\ProgId": {"ProgId": "Applications\\notepad.exe"}}
    assert _with_fake_winreg(reg, fa.reclaim_lb) is True
    assert base not in reg.values and base + "Latest" not in reg.values, \
        "a foreign choice for .lb survived"


def test_lb_already_ours_is_left_alone():
    if sys.platform != "win32":
        return
    base = (r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
            r"\.lb\UserChoice")
    reg = _Reg()
    reg.values = {base: {"ProgId": fa.PROGID, "Hash": "x"}}
    assert _with_fake_winreg(reg, fa.reclaim_lb) is False
    assert base in reg.values


def test_reclaiming_never_touches_pdf_or_powerpoint():
    """Those belong to whichever app the user picks."""
    if sys.platform != "win32":
        return
    root = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
    reg = _Reg()
    for ext in fa.DOCUMENT_TYPES:
        reg.values[root + "\\" + ext + r"\UserChoice"] = {
            "ProgId": "AcroExch.Document"}
    before = {k: dict(v) for k, v in reg.values.items()}
    _with_fake_winreg(reg, fa.reclaim_lb)
    assert reg.values == before, "a PDF/PowerPoint choice was changed"


def test_the_lb_check_runs_on_every_launch():
    """Before the "nothing changed" early return — a choice can be made at
    any time, not only when LogicBoard's own paths change."""
    import inspect
    src = inspect.getsource(fa.register_file_association)
    assert src.index("reclaim_lb()") < src.index("_fingerprint_matches(")


def test_the_uninstaller_forgets_the_one_time_offer():
    src = (ROOT / "LogicBoardInstaller" / "LogicBoardInstaller.cpp").read_text(
        encoding="utf-8", errors="replace")
    assert 'L"%s"' % fa.DOCS_ONCE_VALUE in src, \
        "a reinstall would never offer PDF/PowerPoint again"

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
