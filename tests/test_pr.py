import shutil
import subprocess
from pathlib import Path

import pytest

from driftzero import pr, repair, verify
from driftzero.blast_radius import analyze
from driftzero.guard import guard_check
from driftzero.loop import LoopResult
from driftzero.spec import DriftSpec
from driftzero.telemetry import Cost

ROOT = Path(__file__).resolve().parents[1]
BENCH_REL = "benchmarks/robotics_telemetry_drift"
MUSE_DIFF = ROOT / "docs" / "runs" / "20261030-014828-02a484.diff"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def verified(tmp_path):
    """A verified LoopResult for the real Muse repair, in a repo whose origin is a local bare repo."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "repo"
    shutil.copytree(ROOT / BENCH_REL, root / BENCH_REL, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (root / ".gitignore").write_text(".driftzero/\n__pycache__/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "t")
    git(root, "config", "user.email", "t@t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")

    spec = DriftSpec.load(root / BENCH_REL / "driftspec_v2.json")
    br = analyze(root / BENCH_REL, spec)
    wt = repair.create_worktree(root / BENCH_REL, "p1")
    hidden = repair.hide_in_worktree(wt, [f"{BENCH_REL}/holdout_v2"])
    git(wt.path, "apply", str(MUSE_DIFF))
    g = guard_check(wt, repair.collect_diff(wt))
    v = verify.verify_local(wt.path)
    answer = repair.RepairAnswer(files_changed=["x"], summary="Migrated to quaternions and covariance.",
                                 tests_run=21, tests_passed=21, confidence=0.95)
    (root / ".driftzero" / "runs").mkdir(parents=True, exist_ok=True)
    res = LoopResult(task_id="p1", worktree=wt, spec=spec, blast_radius=br, passed=v.passed and g.ok,
                     iterations=[{"iteration": 1, "guard": g.as_dict()}], log_path=root / ".driftzero" / "runs" / "p1.jsonl",
                     cost=Cost(1393249, 1279572, 30026, 19186, 0.4616), wall_time_s=160.0, model="muse-spark-1.3",
                     steps=22, answer=answer, guard=g, verify=v, hidden_holdout_files=hidden)
    yield res, origin
    repair.remove_worktree(res.worktree)


def test_body_has_every_section(verified):
    res, _ = verified
    body = pr.render_body(res)
    for heading in ("## Summary", "## API change (DriftSpec)", "## Blast radius", "## Test changes, review required",
                    "## Verification", "## Cost", "## Logs"):
        assert heading in body, heading
    assert "Migrated to quaternions and covariance." in body
    assert "| field removed | `TelemetryFrame.noise` |" in body
    assert "**check_frame**" in body and "summarize" in body
    assert "`['/v1/robots/amr-03/telemetry/latest', '/v1/fleet/telemetry']` → `['/v2/robots" in body  # a literal change surfaced for review
    assert "**21/21** passed" in body and "**7/7** passed" in body
    assert "91.8% cached" in body and "**$0.4616**" in body and "| model steps | 22 |" in body
    assert body.count("<details>") == 2 and "local (--local)" in body


def test_commit_push_without_pr(verified):
    res, origin = verified
    out = pr.open_pr(res, create=False)
    assert out.url is None and out.pushed
    assert git(origin, "rev-parse", f"refs/heads/{res.worktree.branch}").strip() == out.commit
    files = git(origin, "show", "--name-status", "--format=", out.commit)
    assert "holdout_v2" not in files  # hidden hold-outs are not deleted by the commit
    assert f"A\t{BENCH_REL}/tests/fixtures/fleet_v2.json" in files
    msg = git(origin, "log", "-1", "--format=%B", out.commit)
    assert msg.startswith(pr.title(res)) and "DriftZero-Task: p1" in msg
    assert res.log_path.with_suffix(".pr.md").exists()


def test_refuses_unverified(verified):
    res, _ = verified
    res.passed = False
    with pytest.raises(ValueError):
        pr.open_pr(res)
