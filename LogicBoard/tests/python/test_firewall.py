"""
test_firewall.py — "can anything actually reach this board?"

Runs under pytest, and also standalone
(``python tests/python/test_firewall.py``).

WHAT IS BEING PROTECTED
-----------------------
Binding a socket succeeds whether or not Windows will deliver to it, so
the service reports ``running`` while a phone gets nothing. The check that
tells them apart is a walk over the firewall rules, and it has two ways to
be confidently wrong — both taken from a real machine:

  * Rules name an exe by PATH, and Windows stores whatever spelling was
    used. The same executable was present as both
    ``C:\\Users\\teacher\\LogicBoard\\LogicBoard.exe`` and
    ``c:\\users\\teacher\\logicboard\\logicboard.exe``. Compare
    case-sensitively and you miss the two rules that were doing the work.

  * Rules carry a PROFILE MASK. That machine had a rule for the installed
    board limited to domain+private (3) while sitting on a public network
    (4). It exists, it is enabled, it is an allow — and it permits
    nothing. Ignore the mask and you report "allowed" for a board nobody
    can reach.

The COM walk itself is not re-tested here; it is exercised against the
real firewall in ``test_the_real_machine_can_be_asked``. What is tested
exhaustively is the decision made about the rules, because that is where
an off-by-one-concept lives.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "..")))

from pathlib import Path

from ui.util import firewall as fw


# ── the two traps ───────────────────────────────────────────────────────────

def test_the_same_exe_spelled_differently_is_the_same_exe():
    exe = Path(r"C:\Users\teacher\LogicBoard\LogicBoard.exe")
    for spelling in (r"C:\Users\teacher\LogicBoard\LogicBoard.exe",
                     r"c:\users\teacher\logicboard\logicboard.exe",
                     r"C:\Users\teacher\LogicBoard\.\LogicBoard.exe",
                     r"C:\Users\teacher\Documents\..\LogicBoard\LogicBoard.exe"):
        assert fw._same_exe(spelling, exe), spelling


def test_a_different_exe_is_not_our_exe():
    exe = Path(r"C:\Users\teacher\LogicBoard\LogicBoard.exe")
    for other in (r"C:\Program Files\LogicBoard\LogicBoard.exe",
                  r"C:\Users\teacher\LogicBoard\LogicShare.exe",
                  r"C:\Users\nalay\LogicBoard\LogicBoard.exe",
                  "", None):
        assert not fw._same_exe(other, exe), other


def test_a_rule_for_another_network_profile_does_not_count():
    """THE one the real machine failed. profiles=3 is domain+private; on a
    public network (4) that rule permits nothing at all."""
    assert not fw._covers(3, 4), \
        "a domain+private rule was counted on a public network"
    assert fw._covers(3, 2), "a domain+private rule ignored on private"
    assert fw._covers(4, 4)
    assert fw._covers(1, 1)


def test_an_any_profile_rule_counts_everywhere():
    for current in (1, 2, 4, 7):
        assert fw._covers(0x7FFFFFFF, current), current


def test_a_nonsense_profile_mask_is_not_treated_as_permission():
    for bad in (None, "", "public", object()):
        assert not fw._covers(bad, 4), bad


# ── the states ──────────────────────────────────────────────────────────────

def test_every_state_has_something_to_say():
    for state in (fw.STATE_ALLOWED, fw.STATE_BLOCKED, fw.STATE_ABSENT,
                  fw.STATE_UNKNOWN, fw.STATE_NA):
        msg = fw.explain(state)
        assert msg and len(msg) > 12, (state, msg)


def test_the_two_states_that_mean_trouble_say_logicshare_will_not_work():
    for state in (fw.STATE_BLOCKED, fw.STATE_ABSENT):
        assert "cannot work" in fw.explain(state), state


def test_unknown_never_claims_a_problem():
    """The check failing is not evidence of a block. Saying so would send a
    teacher after an administrator password to fix nothing."""
    msg = fw.explain(fw.STATE_UNKNOWN).lower()
    assert "block" not in msg and "cannot work" not in msg, msg


# ── the real machine ────────────────────────────────────────────────────────

def test_the_real_machine_can_be_asked():
    """Exercises the actual COM walk. Asserts only that a real answer comes
    back — which answer depends on the machine, and pinning one would make
    this fail on a correctly-configured board."""
    state = fw.inbound_state()
    assert state in (fw.STATE_ALLOWED, fw.STATE_BLOCKED, fw.STATE_ABSENT,
                     fw.STATE_UNKNOWN, fw.STATE_NA), state
    if sys.platform == "win32":
        assert state != fw.STATE_NA


def test_a_path_with_no_rule_at_all_reports_absent():
    if sys.platform != "win32":
        return
    state = fw.inbound_state(Path(r"C:\definitely\not\installed\nope.exe"))
    # UNKNOWN is a legitimate answer on a machine without pywin32; ABSENT is
    # the answer everywhere else. ALLOWED would be a bug.
    assert state in (fw.STATE_ABSENT, fw.STATE_UNKNOWN), state


def test_the_exe_we_ask_about_is_the_one_holding_the_socket():
    """Not the .py file. The firewall knows processes, and from source the
    process is python.exe."""
    exe = fw.app_exe_path()
    assert exe.suffix.lower() == ".exe", exe
    assert str(exe) == str(Path(sys.executable).resolve())


# ── what the settings card does with the answer ─────────────────────────────

def _shown(widget) -> bool:
    """Is this widget set visible?

    ``isHidden()``, not ``isVisible()``. isVisible() is False for every
    child of a window that was never shown, so an assertion built on it
    passes whatever the code does — which is exactly how
    "a healthy board gets no banner" passed here before the card was ever
    shown. isHidden() reports the setVisible() state the code actually
    controls.
    """
    return not widget.isHidden()


def _card():
    """The real settings card, against a stub service."""
    import tempfile
    from PySide6.QtWidgets import QApplication
    global _app
    _app = QApplication.instance() or QApplication([])
    import ui.util.paths as paths
    tmp = Path(tempfile.mkdtemp(prefix="lb_fw_"))
    paths.saves_dir = lambda: tmp
    from ui.logicshare.settings_section import LogicShareSettingsCard
    from ui.logicshare.config import LSConfig

    class Sig:
        def connect(self, *_a, **_k):
            pass

    class Svc:
        enabled = True
        conflict = None
        status_changed = conflict_detected = conflict_cleared = Sig()
        restarted = []

        def status(self):
            return "running"

        def restart(self):
            Svc.restarted.append(1)

        def retry(self):
            pass

    return LogicShareSettingsCard(Svc(), LSConfig(tmp / "ls.json"), None), _app


def _with_state(card, app, state):
    real = fw.inbound_state
    fw.inbound_state = lambda *a, **k: state
    try:
        card.refresh()
        app.processEvents()
    finally:
        fw.inbound_state = real


def test_the_card_warns_when_windows_has_not_allowed_the_board():
    card, app = _card()
    _with_state(card, app, fw.STATE_ABSENT)
    assert _shown(card._fw_box), "no warning shown"
    assert "will not work" in card._fw_msg.text(), card._fw_msg.text()
    assert card._btn_allow.text() == "Allow network access"


def test_the_card_says_BLOCKING_when_a_block_rule_exists():
    card, app = _card()
    _with_state(card, app, fw.STATE_BLOCKED)
    assert _shown(card._fw_box)
    assert "BLOCKING" in card._fw_msg.text(), card._fw_msg.text()


def test_a_healthy_board_gets_no_banner_at_all():
    card, app = _card()
    _with_state(card, app, fw.STATE_ALLOWED)
    assert not _shown(card._fw_box), \
        "a warning is shown on a board that is working"
    assert "Online" in card._v_conn.text()


def test_an_unknown_answer_is_kept_quiet():
    """A check that could not run must not become an accusation."""
    card, app = _card()
    _with_state(card, app, fw.STATE_UNKNOWN)
    assert not _shown(card._fw_box), \
        "the banner appears when the check simply could not run"


def test_the_badge_stops_saying_Online_when_nothing_can_reach_the_board():
    """The contradiction that sends people to look at their WiFi: the
    service IS running, so the pill said Running and the line said Online,
    over a red banner explaining that nobody can reach it."""
    card, app = _card()
    _with_state(card, app, fw.STATE_ABSENT)
    line = card._v_conn.text()
    assert "Online" not in line, line
    assert "block" in line.lower(), line


def test_the_warning_clears_once_windows_allows_it():
    card, app = _card()
    _with_state(card, app, fw.STATE_BLOCKED)
    assert _shown(card._fw_box)
    _with_state(card, app, fw.STATE_ALLOWED)
    assert not _shown(card._fw_box), "the warning is stuck on"


# ═══════════════════════════════════════════════════════════════════════════
# The padlock on the password-gated buttons
# ═══════════════════════════════════════════════════════════════════════════

def test_the_stop_start_toggle_keeps_its_padlock_through_a_refresh():
    """make_btn() builds every gated button with a padlock, and refresh()
    rewrote the Stop/Start label from a bare literal — dropping it. That
    runs on every service-status change, so the button was created with a
    lock and had lost it before the panel was ever looked at, sitting in a
    row where Restart, Edit and Change password all still had theirs.

    Both directions of the toggle are password-gated, so both need it."""
    from ui.logicshare.settings_section import _LOCK
    card, app = _card()
    btn = card._btn_stopstart
    assert btn.text().startswith(_LOCK),         f"built without a padlock: {btn.text()!r}"
    for enabled in (True, False, True):
        card._svc.enabled = enabled
        card.refresh()
        app.processEvents()
        assert btn.text().startswith(_LOCK), (
            "refresh() dropped the padlock with enabled=%s: %r"
            % (enabled, btn.text()))
        assert ("Stop service" if enabled else "Start service") in btn.text()


def test_every_password_gated_button_is_marked_and_the_open_one_is_not():
    """The padlock is the only thing telling a teacher which buttons will
    ask for the password. One unmarked gated button and the mark stops
    meaning anything."""
    from ui.logicshare.settings_section import _LOCK
    card, _app = _card()
    for name in ("_btn_edit", "_btn_changepw", "_btn_restart",
                 "_btn_stopstart"):
        btn = getattr(card, name)
        assert btn.text().startswith(_LOCK),             f"{name} is password-gated but unmarked: {btn.text()!r}"
    assert not card._btn_history.text().startswith(_LOCK),         "View history is not gated and must not claim to be"


# ═══════════════════════════════════════════════════════════════════════════
# "I allowed it and it still says no permission"
# ═══════════════════════════════════════════════════════════════════════════
#
# A fake firewall, so the rule walk can be driven through the exact rule sets
# that went wrong. Rules are plain objects with the COM attribute names.

_EXE = Path(r"C:\Users\teacher\LogicBoard\LogicBoard.exe")
_PUBLIC = 4


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


def _grant(rules):
    """What the elevated grant does to the rule set: clear our blocks, then
    add the two allow rules netsh would add."""
    fw._disable_rules(rules, _EXE)
    rules += [_R("LogicBoard", 1, proto=6), _R("LogicBoard", 1, proto=17)]
    return rules


def test_a_block_with_another_name_no_longer_survives_allow():
    """THE bug. Windows' own popup names its rules "logicboard.exe"; the
    grant only removed rules named "LogicBoard". The block outlived every
    approval, and since a block beats any allow, the banner never cleared."""
    rules = [_R("logicboard.exe", 0, proto=6),
             _R("logicboard.exe", 0, proto=17)]
    assert _state(rules) == fw.STATE_BLOCKED
    _grant(rules)
    assert _state(rules) == fw.STATE_ALLOWED, \
        "approved the prompt and the board is still blocked"


def test_an_outbound_block_is_reported():
    """An outbound block stops the board ANNOUNCING itself, so phones never
    find it. The checker only read inbound rules and called this allowed.
    The development machine had exactly this: "Block LogicBoard Outbound"."""
    rules = [_R("LogicBoard", 1, proto=6), _R("LogicBoard", 1, proto=17),
             _R("Block LogicBoard Outbound", 0, direction=2)]
    assert _state(rules) == fw.STATE_BLOCKED, \
        "an active outbound block was reported as allowed"


def test_allow_clears_an_outbound_block_too():
    rules = [_R("Block LogicBoard Outbound", 0, direction=2)]
    _grant(rules)
    assert _state(rules) == fw.STATE_ALLOWED


def test_blocks_are_disabled_not_deleted():
    """Reversible, and precise: names are not unique (the machine had a dozen
    rules called "logicboard.exe"), so removing by name could take out the
    wrong rule. Disabling acts on the exact rule in hand."""
    rules = [_R("logicboard.exe", 0)]
    before = len(rules)
    fw._disable_rules(rules, _EXE)
    assert len(rules) == before, "a rule was removed"
    assert rules[0].Enabled is False


def test_other_programs_rules_are_left_alone():
    """Same name, different program — the ambiguity that rules out removing
    by name. And an allow for our own exe is not a block."""
    other = _R("logicboard.exe", 0, app=r"C:\old\place\logicboard.exe")
    our_allow = _R("LogicBoard", 1)
    off = fw._disable_rules([other, our_allow], _EXE)
    assert off == []
    assert other.Enabled is True and our_allow.Enabled is True


def test_a_block_on_another_profile_is_cleared_but_not_reported():
    """The checker answers for the network you are on now; a private-only
    block does not stop the board on a public network. But the fixer clears
    it anyway, because it is what bites on the next network."""
    rules = [_R("LogicBoard", 1, proto=6), _R("LogicBoard", 1, proto=17),
             _R("logicboard.exe", 0, profiles=2)]
    assert _state(rules, profile=_PUBLIC) == fw.STATE_ALLOWED
    assert _state(rules, profile=2) == fw.STATE_BLOCKED
    fw._disable_rules(rules, _EXE)
    assert _state(rules, profile=2) == fw.STATE_ALLOWED


def test_a_disabled_block_does_not_count():
    rules = [_R("LogicBoard", 1, proto=6), _R("LogicBoard", 1, proto=17),
             _R("logicboard.exe", 0, enabled=False)]
    assert _state(rules) == fw.STATE_ALLOWED


def test_a_rule_that_refuses_to_change_does_not_stop_the_rest():
    """A Group-Policy rule cannot be changed locally. That must not leave the
    blocks after it in place."""
    class _Locked(_R):
        def __setattr__(self, k, v):
            if k == "Enabled" and hasattr(self, "Enabled"):
                raise PermissionError("managed by policy")
            super().__setattr__(k, v)
    locked = _Locked("GPO block", 0)
    after = _R("logicboard.exe", 0)
    off = fw._disable_rules([locked, after], _EXE)
    assert off == ["logicboard.exe"]
    assert locked.Enabled is True, "the locked rule should be untouched"
    assert after.Enabled is False


def test_the_real_grant_clears_blocks_before_adding_allows():
    """The tests above prove the clearing works; this proves the elevated
    grant actually DOES it, and in the right order. Adding allows over a
    standing block achieves nothing."""
    if sys.platform != "win32":
        return
    import subprocess
    calls = []
    real_run, real_off = subprocess.run, fw._disable_our_blocks

    def fake_run(args, **_k):
        calls.append(("netsh", args[3] if len(args) > 3 else "?"))
        return subprocess.CompletedProcess(args, 0)

    subprocess.run = fake_run
    fw._disable_our_blocks = lambda exe: calls.append(("clear-blocks", "")) or []
    try:
        rc = fw.run_elevated_grant("LogicBoard")
    finally:
        subprocess.run, fw._disable_our_blocks = real_run, real_off
    kinds = [c[0] for c in calls]
    assert "clear-blocks" in kinds, \
        "the elevated grant never clears blocking rules"
    first_add = next(i for i, c in enumerate(calls) if c == ("netsh", "add"))
    assert kinds.index("clear-blocks") < first_add, \
        "allow rules are added before the blocks that override them are cleared"
    assert rc == 0


def test_the_installer_rule_alone_does_not_cover_a_public_network():
    """What a fresh install leaves: one ANY-protocol allow, domain+private.
    On a public network that is genuinely not enough, and saying so is
    correct — the button is what fixes it."""
    rules = [_R("LogicBoard", 1, profiles=3)]
    assert _state(rules) == fw.STATE_ABSENT
    _grant(rules)
    assert _state(rules) == fw.STATE_ALLOWED


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
