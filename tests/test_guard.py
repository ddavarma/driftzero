import shutil
import subprocess
from pathlib import Path

import pytest

from driftzero import repair
from driftzero.guard import FileChange, check_changes, guard_check

ROOT = Path(__file__).resolve().parents[1]
BENCH_REL = "benchmarks/robotics_telemetry_drift"
MUSE_DIFF = ROOT / "docs" / "runs" / "20261030-014828-02a484.diff"

T = "proj/tests/test_x.py"
BASE = '''\
import pytest
from lib import f, g


def helper():
    return 1


def test_a():
    assert f(1) == 2
    assert g("x") == {"k"}


def test_b():
    with pytest.raises(ValueError):
        f(-1)
    assert f(2) == 3


class TestC:
    def test_c(self):
        assert f(0) == 1
'''


def run(new, old=BASE, path=T):
    return check_changes([FileChange(path, old, new)])


def rules(findings):
    return [f.rule for f in findings]


# ---------------------------------------------------------------- hard rejections


def test_reject_test_removed():
    res = run(BASE.replace('def test_b():\n    with pytest.raises(ValueError):\n        f(-1)\n    assert f(2) == 3\n', ""))
    assert not res.ok and rules(res.rejections) == ["test_removed"]
    assert "restore test test_b" in res.rejections[0].message


def test_reject_test_removed_from_class_and_whole_file_deleted():
    assert rules(run(BASE.replace("    def test_c(self):\n        assert f(0) == 1\n", "    pass\n")).rejections) == ["test_removed"]
    res = run(None)
    assert sorted(f.test for f in res.rejections) == ["TestC.test_c", "test_a", "test_b"]


def test_reject_test_renamed():
    assert rules(run(BASE.replace("def test_a", "def test_a_v2")).rejections) == ["test_removed"]


def test_reject_assert_count_decreased():
    res = run(BASE.replace('    assert g("x") == {"k"}\n', ""))
    assert rules(res.rejections) == ["asserts_removed"]
    assert res.rejections[0].message == "restore test test_a; you removed 1 assert (2 -> 1)"


def test_reject_pytest_raises_removed():
    res = run(BASE.replace("    with pytest.raises(ValueError):\n        f(-1)\n", "    f(-1)\n"))
    assert rules(res.rejections) == ["asserts_removed"]


@pytest.mark.parametrize("decorator", ["@pytest.mark.skip", '@pytest.mark.skipif(True, reason="v2")',
                                       '@pytest.mark.xfail(reason="v2")'])
def test_reject_skip_markers(decorator):
    res = run(BASE.replace("def test_a():", f"{decorator}\ndef test_a():"))
    assert rules(res.rejections) == ["skip_added"]


def test_reject_skip_call_and_module_pytestmark():
    assert rules(run(BASE.replace('    assert f(1) == 2\n', '    pytest.skip("later")\n    assert f(1) == 2\n')).rejections) \
        == ["skip_added"]
    res = run(BASE.replace("from lib import f, g\n", "from lib import f, g\n\npytestmark = pytest.mark.xfail\n"))
    assert rules(res.rejections) == ["skip_added"]


def test_reject_assert_replaced_by_bare_expression():
    res = run(BASE.replace("    assert f(1) == 2\n", "    f(1) == 2\n"))
    assert set(rules(res.rejections)) == {"asserts_removed", "bare_expression"}
    # even when the count is topped up elsewhere
    sneaky = BASE.replace("    assert f(1) == 2\n", "    f(1) == 2\n    assert True\n")
    assert rules(run(sneaky).rejections) == ["bare_expression"]


def test_reject_new_conftest():
    res = check_changes([FileChange("proj/conftest.py", None, "def pytest_collection_modifyitems(items):\n    pass\n")])
    assert rules(res.rejections) == ["new_conftest"] and not res.ok


@pytest.mark.parametrize("path,old,new", [
    ("proj/pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
     '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-k not slow"\n'),
    ("proj/pytest.ini", None, "[pytest]\naddopts = -p no:warnings\n"),
    ("proj/setup.cfg", "[metadata]\nname = x\n", "[metadata]\nname = x\n[tool:pytest]\nfilterwarnings = ignore\n"),
])
def test_reject_pytest_config_change(path, old, new):
    assert rules(check_changes([FileChange(path, old, new)]).rejections) == ["pytest_config"]


def test_non_pytest_pyproject_edit_is_fine():
    old = '[project]\nversion = "1.0"\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    res = check_changes([FileChange("proj/pyproject.toml", old, old.replace("1.0", "2.0"))])
    assert res.ok and not res.review_items


# ---------------------------------------------------------------- allowed, reported


def test_review_literal_changes_strings_numbers_and_sets():
    new = BASE.replace("f(1) == 2", "f(1) == 5").replace('{"k"}', '{"k", "health"}').replace('g("x")', 'g("y")')
    res = run(new)
    assert res.ok
    pairs = {(f.old, f.new) for f in res.review_items if f.rule == "literal_change"}
    assert pairs == {("2", "5"), ("'x'", "'y'"), ("{'k'}", "{'k', 'health'}")}


def test_review_dict_expected_value():
    old = "def test_d():\n    assert cfg() == {'a': 1}\n"
    res = run(old.replace("{'a': 1}", "{'a': 1, 'b': 2}"), old=old)
    assert res.ok and rules(res.review_items) == ["literal_change"]


def test_review_fixture_data_edits():
    res = check_changes([FileChange("proj/tests/fixtures/a.json", '{"v": 1}', '{"v": 2}'),
                         FileChange("proj/tests/fixtures/b.json", None, "{}"),
                         FileChange("proj/tests/fixtures/c.json", "{}", None)])
    assert res.ok and [f.message for f in res.review_items] == ["test data edited", "test data added", "test data deleted"]


def test_review_existing_conftest_edit_is_allowed():
    old = "import json\n\ndef fleet():\n    return json.load(open('fleet_v1.json'))\n"
    res = check_changes([FileChange("proj/tests/conftest.py", old, old.replace("v1", "v2"))])
    assert res.ok and rules(res.review_items) == ["literal_change"]


def test_review_comparison_loosening():
    res = run(BASE.replace("assert f(1) == 2", "assert f(1) == pytest.approx(2, abs=0.5)"))
    assert res.ok
    assert "loosened" in rules(res.review_items)
    tol = "def test_t():\n    assert math.isclose(h(), 1.0, abs_tol=1e-9)\n    assert h() == pytest.approx(1.0)\n"
    res = run(tol.replace("pytest.approx(1.0)", "pytest.approx(1.0, rel=0.1)"), old=tol)
    assert res.ok and "loosened" in rules(res.review_items)


def test_review_added_test_and_structural_edit():
    new = BASE.replace("    assert f(2) == 3\n", "    assert f(2) == 3\n    assert f(3) == 4\n") + \
        "\n\ndef test_new():\n    assert f(5) == 6\n"
    res = run(new)
    assert res.ok
    assert set(rules(res.review_items)) == {"modified_test", "added_test"}


def test_non_test_code_is_ignored_and_unchanged_tests_produce_nothing():
    assert check_changes([FileChange("proj/lib.py", "x = 1\n", "x = 2\n")]).review_items == []
    assert run(BASE).review_items == [] and run(BASE).ok


def test_syntax_error_in_test_is_rejected():
    assert rules(run("def test_a(:\n").rejections) == ["syntax_error"]


# ---------------------------------------------------------------- the real Muse repair


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


def test_guard_on_real_muse_repair(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT / BENCH_REL, root / BENCH_REL, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    wt = repair.create_worktree(root / BENCH_REL, "g")
    try:
        git(wt.path, "apply", str(MUSE_DIFF))
        res = guard_check(wt, repair.collect_diff(wt))
        assert res.ok, res.reasons
        got = {(f.rule, f.test) for f in res.review_items}
        assert ("literal_change", "test_client_hits_v1_paths") in got
        assert ("literal_change", "test_noisy_localisation_raises_noisy_alert") in got
        assert ("added_test", "test_negated_quaternion_gives_same_rotation") in got
        assert sum(f.rule == "fixture_data" for f in res.review_items) == 2

        # now cheat on top of it: drop an assert and skip a test
        t = wt.project / "tests" / "test_fusion.py"
        t.write_text(t.read_text().replace("    assert sigma < precise.noise\n", "")
                     .replace("def test_heading_mean", "@pytest.mark.skip\ndef test_heading_mean"))
        bad = guard_check(wt, repair.collect_diff(wt))
        assert not bad.ok and set(rules(bad.rejections)) == {"asserts_removed", "skip_added"}
    finally:
        repair.remove_worktree(wt)
