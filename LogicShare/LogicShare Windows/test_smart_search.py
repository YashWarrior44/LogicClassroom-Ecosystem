"""
test_smart_search.py — the search box understands what a teacher means.

    python test_smart_search.py

Runs the SHARED cases in LogicShare/shared/smart_search_cases.json, the same
file LogicShare Android's SmartSearchTest.kt runs, so the two apps cannot
drift apart on what a query means.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smart_search as S                                  # noqa: E402

CASES = os.path.join(HERE, "..", "shared", "smart_search_cases.json")


def _load():
    with open(CASES, encoding="utf-8") as fh:
        return json.load(fh)


def test_every_shared_board_case():
    data = _load()
    boards = data["boards"]
    bad = []
    for case in data["board_cases"]:
        q = S.parse(case["q"])
        got = sorted(k for k, b in boards.items()
                     if S.match_board(q, b["class"], b["section"], b["room"], b["ip"]))
        if got != sorted(case["match"]):
            bad.append(f"{case['q']!r}: expected {case['match']}, got {got}  "
                       f"(parsed {q.to_dict()})")
    assert not bad, "\n      " + "\n      ".join(bad)


def test_every_shared_history_case():
    data = _load()
    hist = data["history"]
    bad = []
    for case in data["history_cases"]:
        q = S.parse(case["q"])
        got = sorted(k for k, h in hist.items()
                     if S.match_history(q, h["title"], h["message"],
                                        [tuple(r) for r in h["recipients"]]))
        if got != sorted(case["match"]):
            bad.append(f"{case['q']!r}: expected {case['match']}, got {got}  "
                       f"(parsed {q.to_dict()})")
    assert not bad, "\n      " + "\n      ".join(bad)


def test_one_typo_is_tolerated_and_two_are_not():
    assert S._edit1("clas", "class")
    assert S._edit1("romm", "room")
    assert S._edit1("sectoin", "section")         # transposition
    assert not S._edit1("cls", "class")           # two edits
    assert not S._edit1("bloom", "room")


def test_a_hostile_query_cannot_crash_the_search():
    for q in ["((((", "\\", "[a-z]+", "*" * 500, "\x00", "🙂🙂", "....", "---"]:
        S.match_board(S.parse(q), "11", "A", "12", "10.0.0.1")
        S.match_history(S.parse(q), "t", "m", [("11", "A", "12")])


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
