"""
test_firewall.py — LogicShare says when Windows won't let it see the boards,
and its "Allow network access" button gets the permission properly.

    python test_firewall.py

firewall.py is vendored from LogicBoard (ui/util/firewall.py). The rule
logic is checked here to be IDENTICAL to LogicBoard's — two copies of a
fix that drift apart is how one app gets the bug back — and the decisions
are re-tested against fake rule sets taken from real machines.
"""
import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import firewall as fw                                    # noqa: E402

_LB_COPY = HERE.parents[1] / "LogicBoard" / "ui" / "util" / "firewall.py"
_SHARED = ("_same_exe", "_covers", "inbound_state", "_is_our_block", "_query",
           "_disable_rules", "_disable_our_blocks", "run_elevated_grant",
           "app_exe_path")

_EXE = Path(r"C:\Users\teacher\AppData\Local\Programs\LogicShare\LogicShare.exe")
_PUBLIC = 4


# ── the vendored copy is the same logic ────────────────────────────────────

def _bodies(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            # Docstrings may differ (each names its own app); code may not.
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            out[node.name] = ast.dump(ast.Module(body=body, type_ignores=[]))
    return out


def test_the_rule_logic_is_identical_to_logicboards():
    if not _LB_COPY.exists():
        return
    mine, theirs = _bodies(HERE / "firewall.py"), _bodies(_LB_COPY)
    drift = [n for n in _SHARED if mine.get(n) != theirs.get(n)]
    assert not drift, "firewall.py has drifted from LogicBoard's in: %s" % drift


def test_the_default_rule_is_named_for_logicshare():
    import inspect
    sig = inspect.signature(fw.request_access)
    assert sig.parameters["rule_name"].default == "LogicShare"


# ── decisions on real-shaped rule sets ─────────────────────────────────────

class _R:
    def __init__(self, name, action, direction=1, proto=256,
                 profiles=0x7FFFFFFF, app=str(_EXE), enabled=True):
        self.Name, self.Action, self.Direction = name, action, direction
        self.Protocol, self.Profiles = proto, profiles
        self.ApplicationName, self.Enabled = app, enabled


class _FakeCom:
    def __init__(self, rules, profile=_PUBLIC):
        self.policy = type("P", (), {"CurrentProfileTypes": profile,
                                     "Rules": rules})()

    def Dispatch(self, _progid):
        return self.policy


def _state(rules, profile=_PUBLIC):
    return fw._query(_FakeCom(rules, profile), _EXE)


def test_cancel_on_the_windows_popup_is_reported_as_blocked():
    """Pressing Cancel on the first-run prompt writes BLOCK rules named after
    the exe — exactly what leaves LogicShare finding no boards."""
    rules = [_R("logicshare.exe", 0, proto=6), _R("logicshare.exe", 0, proto=17)]
    assert _state(rules) == fw.STATE_BLOCKED


def test_no_rule_at_all_is_reported_absent():
    assert _state([]) == fw.STATE_ABSENT


def test_tcp_without_udp_is_not_enough():
    """UDP carries the mDNS answers that make boards appear at all."""
    assert _state([_R("LogicShare", 1, proto=6)]) == fw.STATE_ABSENT


def test_a_private_only_rule_does_not_count_on_a_public_network():
    assert _state([_R("LogicShare", 1, profiles=3)]) == fw.STATE_ABSENT
    assert _state([_R("LogicShare", 1, profiles=3)], profile=2) == fw.STATE_ALLOWED


def test_the_grant_clears_blocks_and_then_it_is_allowed():
    rules = [_R("logicshare.exe", 0, proto=6), _R("logicshare.exe", 0, proto=17)]
    fw._disable_rules(rules, _EXE)
    rules += [_R("LogicShare", 1, proto=6), _R("LogicShare", 1, proto=17)]
    assert _state(rules) == fw.STATE_ALLOWED


def test_the_messages_say_what_is_wrong():
    for st in (fw.STATE_BLOCKED, fw.STATE_ABSENT):
        msg = fw.explain(st)
        assert "LogicShare" in msg and "boards" in msg, msg
    assert "block" not in fw.explain(fw.STATE_UNKNOWN).lower()


def test_the_elevated_relaunch_runs_logicshare_itself():
    """UAC should show LogicShare (frozen) — and from source, LogicShare.py
    with the flag LogicShare.py handles before any UI exists."""
    import inspect
    src = inspect.getsource(fw.request_access)
    assert 'with_name("LogicShare.py")' in src
    main = (HERE / "LogicShare.py").read_text(encoding="utf-8")
    i = main.index('if __name__ == "__main__":')
    tail = main[i:]
    flag = tail.index("--grant-firewall-access")
    assert flag < tail.index("QApplication(sys.argv)"), \
        "the elevated grant would build a UI before writing the rules"
    assert "firewall.run_elevated_grant(" in tail[flag:flag + 600]


# ── the banner ─────────────────────────────────────────────────────────────

def _banner():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import LogicShare as L
    return L, L.FirewallBanner()


def test_the_banner_warns_for_blocked_and_absent():
    L, b = _banner()
    for st in (fw.STATE_BLOCKED, fw.STATE_ABSENT):
        b.apply_state(st)
        assert not b.isHidden(), st
        assert not b.allow_btn.isHidden() and b.allow_btn.isEnabled()
        assert "boards" in b._msg.text()


def test_the_banner_stays_quiet_when_allowed_or_unknown():
    L, b = _banner()
    for st in (fw.STATE_ALLOWED, fw.STATE_UNKNOWN, fw.STATE_NA):
        b.apply_state(fw.STATE_BLOCKED)
        b.apply_state(st)
        assert b.isHidden(), st


def test_allow_success_shows_it_and_asks_for_a_rescan():
    L, b = _banner()
    b.apply_state(fw.STATE_BLOCKED)
    rescans = []
    b.granted.connect(lambda: rescans.append(1))
    b._on_granted(True)
    assert rescans == [1], "a successful grant did not trigger a rescan"
    assert "allowed" in b._title.text().lower()
    assert b.allow_btn.isHidden()
    assert b.state() == fw.STATE_ALLOWED


def test_allow_failure_keeps_the_warning_and_offers_another_go():
    L, b = _banner()
    b.apply_state(fw.STATE_BLOCKED)
    checks = []
    b.check = lambda: checks.append(1)
    b._on_granted(False)
    assert not b.isHidden()
    assert b.allow_btn.isEnabled() and b.allow_btn.text() == "Try again"
    assert checks, "the state was not re-read after a declined prompt"


def test_the_button_really_runs_the_grant():
    L, b = _banner()
    b.apply_state(fw.STATE_BLOCKED)
    calls = []
    saved = fw.request_access
    fw.request_access = lambda *a, **k: calls.append(1) or True
    try:
        b.allow_btn.click()
        b._granter.wait(5000)
        from PySide6.QtWidgets import QApplication
        for _ in range(20):
            QApplication.processEvents()
    finally:
        fw.request_access = saved
    assert calls == [1]
    assert b.state() == fw.STATE_ALLOWED


def test_the_installer_build_bundles_what_the_check_needs():
    pkg = (HERE / "package.py").read_text(encoding="utf-8")
    for mod in ("firewall", "pythoncom", "win32com.client",
                "win32com.shell.shell", "win32event", "win32process"):
        assert f'"--hidden-import", "{mod}"' in pkg, mod


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
    sys.stdout.flush()
    os._exit(1 if failures else 0)
