import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from driftzero import loop, repair, verify
from driftzero.spec import DriftSpec

ROOT = Path(__file__).resolve().parents[1]
BENCH_REL = "benchmarks/robotics_telemetry_drift"
BENCH = ROOT / BENCH_REL
SPEC_PATH = BENCH / "driftspec_v2.json"
MUSE_DIFF = ROOT / "docs" / "runs" / "20261030-014828-02a484.diff"  # the live Muse v1->v2 repair

needs_muse = pytest.mark.skipif(shutil.which("muse") is None, reason="muse CLI not on PATH")


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """Same layout as the real repo: project at benchmarks/robotics_telemetry_drift, hold-outs committed."""
    root = tmp_path / "repo"
    shutil.copytree(BENCH, root / BENCH_REL, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info"))
    (root / ".gitignore").write_text(".driftzero/\n__pycache__/\n.pytest_cache/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def wt(repo):
    w = repair.create_worktree(repo / BENCH_REL, "t")
    yield w
    repair.remove_worktree(w)


def test_holdouts_fail_on_v1_code(wt):
    res = verify.verify_local(wt.path)
    assert res.project_rel == BENCH_REL
    assert res.agent.exit_code == 0 and res.agent.tests == 18
    assert res.holdout.tests == 7 and res.holdout.exit_code == 1
    assert len(res.holdout.failed_ids()) == 5
    assert not res.passed


def test_muse_repair_passes_both_suites(wt):
    git(wt.path, "apply", str(MUSE_DIFF))
    res = verify.verify_local(wt.path)
    assert (res.agent.passed_count, res.agent.tests) == (21, 21)
    assert (res.holdout.passed_count, res.holdout.tests) == (7, 7)
    assert res.passed


def test_holdouts_cannot_be_tampered_with_from_the_worktree(wt):
    proj = wt.project
    # a root conftest that would deselect everything, a fake hold-out dir, and pytest addopts
    (proj / "conftest.py").write_text("def pytest_collection_modifyitems(config, items):\n    items.clear()\n")
    shutil.rmtree(proj / "holdout_v2")
    (proj / "holdout_v2").mkdir()
    (proj / "holdout_v2" / "test_fake.py").write_text("def test_ok():\n    assert True\n")
    pp = proj / "pyproject.toml"
    pp.write_text(pp.read_text().replace('testpaths = ["tests"]', 'testpaths = ["tests"]\naddopts = "-k nothing"'))
    res = verify.verify_local(wt.path)
    assert res.holdout.tests == 7  # the real hold-outs from the source checkout ran
    assert res.holdout.exit_code == 1  # and still fail on v1 code
    assert res.agent.tests == 0  # the agent's own suite *is* affected by its conftest; the guard (stage 6) handles that


def test_hide_in_worktree_hides_holdouts_without_a_diff(wt):
    hidden = repair.hide_in_worktree(wt, [f"{BENCH_REL}/holdout_v2"])
    assert hidden == [f"{BENCH_REL}/holdout_v2/test_contract_v2.py"]
    assert not (wt.project / "holdout_v2").exists()
    assert repair.collect_diff(wt).files == []
    # verification still finds them in the source checkout
    assert verify.resolve_holdout(wt.path)[0] == wt.root / BENCH_REL / "holdout_v2"


def test_prior_failure_is_head_limited():
    out = "\n".join(f"line {i}" for i in range(400))
    suite = verify.SuiteResult(name="holdout", command=[], exit_code=1, stdout=out, stderr="", junit_xml=None, tests=7,
                               failures=5)
    agent = suite.model_copy(update={"name": "agent", "exit_code": 0, "failures": 0, "stdout": "ok"})
    res = verify.VerifyResult(passed=False, backend="local", project_rel=".", agent=agent, holdout=suite,
                              setup={"exit_code": 0}, wall_time_s=0)
    text = loop.build_prior_failure(res)
    assert "line 149" in text and "line 150" not in text and "(250 more lines)" in text
    assert "hold-out contract tests (not in your worktree): exit 1, 2/7 passed" in text


@needs_muse
def test_loop_dry_run_logs_every_iteration(repo):
    res = loop.repair_until_verified(repo / BENCH_REL, SPEC_PATH, max_iterations=2, dry_run=True, local=True,
                                     on_line=lambda s: None)
    try:
        assert not res.passed and len(res.iterations) == 2
        events = [json.loads(line) for line in res.log_path.read_text().splitlines()]
        assert [e["event"] for e in events] == ["start", "iteration", "iteration", "end"]
        assert events[0]["hidden_from_agent"] == 1
        it = events[1]
        assert it["guard"]["ok"] and it["cost"]["provider"] == "echo" and "input_tokens" in it["cost"]
        assert it["verify"]["holdout"] == {"exit_code": 1, "tests": 7, "passed": 2,
                                           "failed": it["verify"]["holdout"]["failed"]}
        runs = res.log_path.parent
        assert "Previous attempt failed verification" not in (runs / f"{res.task_id}.prompt-i1.md").read_text()
        retry = (runs / f"{res.task_id}.prompt-i2.md").read_text()
        assert "Previous attempt failed verification" in retry and "test_client_requests_v2_paths" in retry
        assert (runs / f"{res.task_id}.muse-i2.jsonl").exists()
    finally:
        repair.remove_worktree(res.worktree)


@pytest.mark.live
@pytest.mark.skipif(os.getenv("DRIFTZERO_LIVE") != "1", reason="Modal sandbox; set DRIFTZERO_LIVE=1 to run")
def test_modal_verify_matches_local(wt):
    git(wt.path, "apply", str(MUSE_DIFF))
    res = verify.verify_in_modal(wt.path, on_line=lambda s: None)
    assert res.backend == "modal" and res.sandbox_id
    assert res.setup["exit_code"] == 0
    assert (res.agent.passed_count, res.holdout.passed_count) == (21, 7) and res.passed
