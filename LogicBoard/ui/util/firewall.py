"""
firewall.py — does Windows actually let anyone reach this board?

WHY THIS EXISTS
===============
Binding a listening socket SUCCEEDS whether or not the Windows firewall
will deliver anything to it. ``LogicShareService.start()`` binds
0.0.0.0:16384, gets no error, and reports ``running`` — while the firewall
silently drops every packet a phone sends. The teacher sees a green
"Running" badge and an app that does not work, with nothing anywhere
saying why.

So "am I allowed" has to be asked separately, and it is a question about
RULES rather than about sockets.

WHAT "ALLOWED" ACTUALLY MEANS
=============================
More than "a rule exists". Read off a real machine while writing this,
which had EIGHTEEN rules naming LogicBoard — one per install location
it had ever occupied, because to the firewall an exe's path is its
identity. Three of them were for the exe actually installed, written
two different ways, and only two of the three did anything:

    profiles=4  TCP  c:\\users\\teacher\\logicboard\\logicboard.exe
    profiles=4  UDP  c:\\users\\teacher\\logicboard\\logicboard.exe
    profiles=3  ANY  C:\\Users\\teacher\\LogicBoard\\LogicBoard.exe

The machine was on profile 4 (public). ``profiles=3`` is domain plus
private, so that third rule — the one spelled the way the installer
writes it — covered nothing at all. The board was reachable only
because of the two lowercase entries an earlier version left behind.

Both halves of that are traps. Compare the path case-sensitively and
you miss the two rules doing the work; ignore the profile mask and you
count a rule switched off for the network you are on. Either mistake
produces a confident answer that is wrong in a different direction.

Hence all of these have to hold, for the profile the machine is on
RIGHT NOW:

    the rule names OUR exe          (path identity)
    direction is inbound            (for the ALLOW rules)
    it is enabled
    it is an ALLOW, and no BLOCK rule matches — a block wins in Windows,
      and in EITHER direction: an outbound block stops the board
      announcing itself over mDNS, so phones never find it
    it covers the CURRENT profile   (the one the eighteen failed)

and both TCP and UDP, because LogicShare needs the HTTP port and mDNS
discovery, and one without the other is a board that either cannot be
found or cannot be sent to.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["app_exe_path", "inbound_state", "request_access",
           "STATE_ALLOWED", "STATE_BLOCKED", "STATE_ABSENT",
           "STATE_UNKNOWN", "STATE_NA", "explain"]

# ── the answers this module can give ─────────────────────────────────────
STATE_ALLOWED = "allowed"   # inbound TCP and UDP are permitted right now
STATE_BLOCKED = "blocked"   # an explicit BLOCK rule matches — the worst case
STATE_ABSENT = "absent"     # nothing permits us on this network profile
STATE_UNKNOWN = "unknown"   # could not ask (no COM, query failed)
STATE_NA = "n/a"            # not Windows; there is no such thing to check

# Windows Firewall constants. Named rather than inlined because a bare 1
# meaning "inbound" in one place and "allow" in another is exactly the kind
# of thing that reads fine and is wrong.
_DIR_IN = 1
_DIR_OUT = 2
_ACTION_BLOCK = 0
_ACTION_ALLOW = 1
_PROTO_TCP = 6
_PROTO_UDP = 17
_PROTO_ANY = 256
_PROFILE_ALL = 0x7FFFFFFF


def app_exe_path() -> Path:
    """The executable Windows would write a firewall rule about.

    ``sys.executable`` either way. Frozen, that is LogicBoard.exe. From
    source it is python.exe — which looks odd but is the truth: python.exe
    is the process holding the socket, and it is python.exe the firewall
    prompts about. Reporting the .py file instead would produce a check
    that never matches anything.
    """
    try:
        return Path(sys.executable).resolve()
    except Exception:
        return Path(sys.executable) if sys.executable else Path()


def _same_exe(rule_app: str, exe: Path) -> bool:
    """Do a rule's ApplicationName and our exe mean the same file?

    Compared case-insensitively on the normalised path. Windows stores
    these however they were written — the machine this was built against
    held the same executable as both ``C:\\Users\\teacher\\LogicBoard\\
    LogicBoard.exe`` and ``c:\\users\\teacher\\logicboard\\logicboard.exe``.
    """
    if not rule_app:
        return False
    try:
        a = os.path.normcase(os.path.normpath(str(rule_app)))
        b = os.path.normcase(os.path.normpath(str(exe)))
        return a == b
    except Exception:
        return False


def _covers(profiles: int, current: int) -> bool:
    """Does a rule's profile mask apply to the network we are on now?

    THE check the eighteen stale rules failed. ``profiles`` is a bitmask
    (1 domain, 2 private, 4 public) and ``_PROFILE_ALL`` is the "any"
    value Windows writes for a rule created without a profile filter.
    """
    try:
        p = int(profiles)
    except (TypeError, ValueError):
        return False
    if p == _PROFILE_ALL:
        return True
    return bool(p & int(current or 0))


def inbound_state(exe: Optional[Path] = None) -> str:
    """One of the STATE_* values, for the network profile in use now."""
    if sys.platform != "win32":
        return STATE_NA
    exe = Path(exe) if exe else app_exe_path()
    if not str(exe):
        return STATE_UNKNOWN

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        # No pywin32 in this build. Say so rather than guessing: claiming
        # "allowed" would hide a real problem and claiming "absent" would
        # nag a user whose firewall is perfectly fine.
        logger.debug("pywin32 unavailable; cannot read the firewall")
        return STATE_UNKNOWN

    initialised = False
    try:
        try:
            pythoncom.CoInitialize()
            initialised = True
        except Exception:
            pass            # already initialised on this thread, which is fine
        # The COM work is nested so that every interface pointer it holds
        # goes out of scope before CoUninitialize runs in the finally.
        # Releasing one afterwards prints "Win32 exception occurred
        # releasing IUnknown" to stderr — harmless, and alarming enough in
        # a log that someone would go looking for a fault that is not there.
        return _query(win32com.client, exe)
    except Exception:
        logger.debug("firewall query failed", exc_info=True)
        return STATE_UNKNOWN
    finally:
        if initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _is_our_block(rule, exe: Path) -> bool:
    """An enabled BLOCK rule, either direction, naming our exe.

    The one definition shared by the checker and the fixer, so they cannot
    disagree about what is in the way — they did. The checker counted any
    inbound block for our exe; ``run_elevated_grant`` only removed rules
    NAMED "LogicBoard". A block under any other name — "logicboard.exe" is
    what Windows' own popup writes when someone clicks Cancel — survived
    every press of "Allow network access", and because a block beats every
    allow, the banner stayed up no matter how many times the teacher
    approved the prompt.

    Outbound counts too. LogicShare is not only inbound: the board SENDS
    its mDNS announcements and its acknowledgments, so an outbound block
    leaves a board phones cannot find, behind a check that used to say
    "allowed". Found on the development machine as an enabled rule named
    "Block LogicBoard Outbound".

    Profile is deliberately NOT considered here — see the callers: the
    checker filters by the current profile itself, and the fixer clears all
    of them, because a block on the profile you are not on today is the one
    that bites on the next network.
    """
    try:
        if rule.Action != _ACTION_BLOCK or not rule.Enabled:
            return False
        if rule.Direction not in (_DIR_IN, _DIR_OUT):
            return False
        return _same_exe(getattr(rule, "ApplicationName", "") or "", exe)
    except Exception:
        return False


def _query(win32com_client, exe: Path) -> str:
    """The rule walk itself. Separate so its COM references die first."""
    policy = win32com_client.Dispatch("HNetCfg.FwPolicy2")
    current = policy.CurrentProfileTypes
    tcp = udp = False
    try:
        for rule in policy.Rules:
            try:
                if _is_our_block(rule, exe) and _covers(rule.Profiles, current):
                    # A block beats every allow in Windows' own precedence,
                    # in either direction, so there is nothing left to look
                    # for.
                    return STATE_BLOCKED
                if rule.Direction != _DIR_IN or not rule.Enabled:
                    continue
                if not _same_exe(getattr(rule, "ApplicationName", "") or "",
                                 exe):
                    continue
                if not _covers(rule.Profiles, current):
                    continue
                if rule.Action != _ACTION_ALLOW:
                    continue
                proto = rule.Protocol
                if proto in (_PROTO_TCP, _PROTO_ANY):
                    tcp = True
                if proto in (_PROTO_UDP, _PROTO_ANY):
                    udp = True
            except Exception:
                # One unreadable rule out of several hundred is not a
                # reason to give up on the question.
                continue
    finally:
        policy = None
    # Both, or it does not count: TCP carries the transfer and UDP carries
    # the mDNS discovery that lets a phone find the board at all. Either
    # one alone is a board that cannot be used.
    return STATE_ALLOWED if (tcp and udp) else STATE_ABSENT


def explain(state: str) -> str:
    """One line a teacher can act on."""
    return {
        STATE_ALLOWED: "Windows is letting devices reach this board.",
        STATE_BLOCKED: ("Windows is BLOCKING this board on the current "
                        "network, so LogicShare cannot work."),
        STATE_ABSENT: ("Windows has not been told to let devices reach this "
                       "board on the current network, so LogicShare cannot "
                       "work."),
        STATE_UNKNOWN: "Could not check what Windows is allowing.",
        STATE_NA: "Not applicable on this system.",
    }.get(state, "Could not check what Windows is allowing.")


def request_access(exe: Optional[Path] = None, *,
                   rule_name: str = "LogicBoard") -> bool:
    """Ask Windows — via the user — to allow this board through.

    Adding a firewall rule needs administrator rights, so this raises the
    standard consent prompt through "runas" and lets the user answer it.
    Returns True if the rule is in place afterwards.

    WHY NOT JUST RE-TRIGGER WINDOWS' OWN "allow access?" POPUP
    ---------------------------------------------------------
    Because it cannot be re-triggered. Windows offers that dialog the
    first time a program listens with no rule present, and once an answer
    has been recorded — including "Cancel", which writes a BLOCK — it is
    never offered again. A teacher who dismissed it once has no way back
    to it, which is the situation this function exists for.

    The rules written here are what that dialog would have written, with
    two differences that matter: ``profile=any`` rather than whichever
    profile happened to be active, and both protocols. The machine this
    was built against had a rule for the installed exe limited to
    domain+private while it sat on a public network, which is a rule that
    exists and does nothing.

    WHY THIS ELEVATES *OURSELVES*, NOT A SCRIPT
    ---------------------------------------------
    This used to write a temp .bat and elevate THAT with ShellExecuteEx.
    Two things went wrong with it:

    1. UAC identifies the elevated program from the file being launched.
       A .bat is handled by cmd.exe, so the consent prompt read "Windows
       Command Processor" — a bare terminal, with nothing on it saying
       LogicBoard has anything to do with the request.
    2. Elevating a script goes through the .bat file association, which
       Windows resolves via an intermediate hop before the real cmd.exe
       takes over. The ``hProcess`` ShellExecuteEx hands back for that
       case isn't reliably the one that runs to completion —
       WaitForSingleObject could return before the netsh calls inside
       the script had actually finished, so this function would go on
       to delete the temp .bat and check the firewall state while the
       script might still be mid-run. That is what made "I clicked
       Allow and it still says blocked" possible even with correct
       netsh commands.

    Elevating a real, compiled executable fixes both: re-launching
    ourselves with ``--grant-firewall-access`` (see ``run_elevated_grant``
    and ``ui.app.run``) makes UAC show LogicBoard's own name and icon on
    a frozen build — the only case a teacher on a classroom smart board
    ever sees — and the process handle is unambiguous: it IS LogicBoard,
    so waiting on it means waiting for the netsh calls that same elevated
    process runs before it exits. This mirrors how
    ``ui.util.updater.launch_installer`` already elevates
    LogicBoardInstaller.exe directly rather than a script.
    """
    if sys.platform != "win32":
        return False
    exe = Path(exe) if exe else app_exe_path()
    if not exe or not str(exe):
        return False

    frozen = getattr(sys, "frozen", False)
    if frozen:
        # sys.executable IS LogicBoard.exe — elevate it directly so UAC
        # shows our own identity, not a terminal's.
        launch_target = str(sys.executable)
        params = f'--grant-firewall-access "{rule_name}"'
        workdir = str(Path(sys.executable).resolve().parent)
    else:
        # Running from source: elevate the current interpreter with
        # -m ui.app, the same convention ui.util.file_association uses
        # for its "open with LogicBoard" command, so this keeps working
        # under a venv/conda interpreter the same way that one does.
        project_root = Path(__file__).resolve().parents[2]
        launch_target = str(sys.executable)
        params = f'-m ui.app --grant-firewall-access "{rule_name}"'
        workdir = str(project_root)

    try:
        import pythoncom          # noqa: F401  (ensures pywin32 is present)
        import win32com.shell.shell as shell      # type: ignore
        import win32event         # type: ignore
        import win32process       # type: ignore
        import win32con           # type: ignore

        res = shell.ShellExecuteEx(
            lpVerb="runas", lpFile=launch_target, lpParameters=params,
            lpDirectory=workdir, nShow=win32con.SW_HIDE,
            fMask=0x00000040)          # SEE_MASK_NOCLOSEPROCESS
        handle = res["hProcess"]
        # The user may sit on the consent prompt; there is no useful
        # timeout shorter than "as long as they take". Because this is
        # our own exe (not a script behind a file association), this
        # handle reliably refers to the process that runs the netsh
        # calls, so waiting on it means they have actually finished.
        win32event.WaitForSingleObject(handle, win32event.INFINITE)
        win32process.GetExitCodeProcess(handle)
    except Exception:
        # Declining the prompt lands here too — ShellExecuteEx raises
        # rather than returning a code — and a decline is an answer, not
        # an error worth a traceback in the log.
        logger.debug("the elevated firewall request did not complete",
                     exc_info=True)

    # The only answer worth reporting is what the firewall says now.
    return inbound_state(exe) == STATE_ALLOWED


def _disable_rules(rules, exe: Path) -> list:
    """Switch off every rule in ``rules`` that ``_is_our_block`` matches.

    DISABLED, not deleted, for two reasons:

    1. Precision. The COM collection removes rules by NAME, and names are
       not unique — the development machine had a dozen rules all called
       "logicboard.exe", for a dozen different paths. Removing by name
       could take out the wrong one. Setting ``Enabled`` acts on the exact
       rule object in hand.
    2. It is reversible. Someone may have put that block there on purpose;
       a disabled rule is still in Windows Defender Firewall for them to
       switch back on, with its name and settings intact.

    Returns the names of the rules switched off.
    """
    off = []
    for rule in rules:
        try:
            if _is_our_block(rule, exe):
                rule.Enabled = False
                off.append(str(getattr(rule, "Name", "") or "?"))
        except Exception:
            # A rule we may not change — one pushed by Group Policy, say —
            # stays as it is. The final state check reports the outcome.
            logger.debug("could not disable a blocking rule", exc_info=True)
    return off


def _disable_our_blocks(exe: Path) -> list:
    """The elevated half: open the real policy and clear our blocks."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return []
    initialised = False
    try:
        try:
            pythoncom.CoInitialize()
            initialised = True
        except Exception:
            pass
        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        try:
            off = _disable_rules(policy.Rules, exe)
        finally:
            policy = None
        if off:
            logger.info("Disabled firewall rule(s) blocking %s: %s", app_exe_path().stem,
                        ", ".join(off))
        return off
    except Exception:
        logger.exception("could not clear blocking firewall rules")
        return []
    finally:
        if initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def run_elevated_grant(rule_name: str) -> int:
    """Write the inbound TCP+UDP allow rules for our own exe. Returns a
    process exit code (0 success, 1 failure).

    This is the OTHER end of ``request_access``: it runs inside the
    elevated re-launch, after UAC has already approved US specifically
    (not a terminal). ``ui.app.run`` calls this and exits immediately
    when started with ``--grant-firewall-access <rule_name>`` — there is
    no UI for this invocation.

    Uses ``subprocess`` with an argument list rather than a shell string,
    so an exe path containing spaces or non-ASCII characters (this
    project's install path has both, in the credit line's emoji) is
    passed as a single argument without needing any manual quoting —
    the exact class of problem a hand-quoted command line used to risk.
    """
    if sys.platform != "win32":
        return 1
    exe = str(app_exe_path())

    import subprocess
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE — no console flash

    def _netsh(*args: str) -> bool:
        try:
            r = subprocess.run(
                ["netsh", "advfirewall", "firewall", *args],
                startupinfo=startupinfo, capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            logger.exception("netsh call failed: %s", args)
            return False

    # Blocks first. Adding allow rules over a block achieves nothing — the
    # block wins — and that is precisely how "I allowed it and it still says
    # no permission" happened.
    _disable_our_blocks(Path(exe))

    _netsh("delete", "rule", f"name={rule_name}", f"program={exe}")
    ok_tcp = _netsh("add", "rule", f"name={rule_name}", "dir=in",
                    "action=allow", f"program={exe}", "enable=yes",
                    "profile=any", "protocol=tcp")
    ok_udp = _netsh("add", "rule", f"name={rule_name}", "dir=in",
                    "action=allow", f"program={exe}", "enable=yes",
                    "profile=any", "protocol=udp")
    return 0 if (ok_tcp and ok_udp) else 1