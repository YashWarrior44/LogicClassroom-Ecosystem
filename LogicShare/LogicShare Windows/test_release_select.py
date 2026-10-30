"""
test_release_select.py — LogicShare picks ITS release out of the shared
ecosystem repo, never LogicBoard's.

    python test_release_select.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import LogicShare as L          # noqa: E402


def _rel(tag, *names, pre=False):
    return {"tag_name": tag, "prerelease": pre, "draft": False,
            "assets": [{"name": n, "browser_download_url": "https://x/" + n,
                        "size": 5} for n in names]}


def test_it_points_at_the_shared_repo_and_lists_releases():
    assert L.GITHUB_REPOSITORY == "YashWarrior44/LogicClassroom-Ecosystem"
    assert "/releases/latest" not in L._GITHUB_LATEST_TMPL


def test_logicboards_release_is_never_offered():
    data = [_rel("LogicBoard-v9.0.0", "LogicBoardInstaller.exe"),
            _rel("LogicShare-v1.1.2", "LogicShareInstaller.exe", "LogicShare.apk")]
    r, why = L._select_release(data)
    assert why is None and r["tag_name"] == "LogicShare-v1.1.2"


def test_the_highest_version_wins_whatever_the_order():
    data = [_rel("LogicShare-v1.1.2", "LogicShareInstaller.exe"),
            _rel("LogicShare-v1.2.0", "LogicShareInstaller.exe"),
            _rel("LogicShare-v1.1.9", "LogicShareInstaller.exe")]
    assert L._select_release(data)[0]["tag_name"] == "LogicShare-v1.2.0"


def test_an_apk_only_release_and_a_prerelease_are_passed_over():
    data = [_rel("LogicShare-v1.4.0", "LogicShare.apk"),
            _rel("LogicShare-v1.3.0", "LogicShareInstaller.exe", pre=True),
            _rel("LogicShare-v1.2.0", "LogicShareInstaller.exe")]
    assert L._select_release(data)[0]["tag_name"] == "LogicShare-v1.2.0"


def test_nothing_tagged_for_logicshare_says_so():
    r, why = L._select_release([_rel("LogicBoard-v3.1.3", "LB.exe")])
    assert r is None and "No LogicShare release" in why


def test_the_tag_prefix_does_not_confuse_the_version_parser():
    assert L._parse_version("LogicShare-v1.1.2") == (1, 1, 2)


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
