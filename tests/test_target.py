import json
import shutil
import subprocess
from pathlib import Path

import pytest

from driftzero import loop, pr, repair, target, verify
from driftzero.blast_radius import analyze
from driftzero.guard import guard_check
from driftzero.spec import DriftSpec
from driftzero.telemetry import Cost

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "robotics_telemetry_drift"
MUSE_DIFF = ROOT / "docs" / "runs" / "20261030-014828-02a484.diff"  # paths prefixed benchmarks/robotics_telemetry_drift/

needs_muse = pytest.mark.skipif(shutil.which("muse") is None, reason="muse CLI not on PATH")


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path):
    """A target repo published at a file:// URL (like ddavarma/fleet-telemetry-v1: no hold-outs, no DriftSpec),
    plus a DriftZero 'home' holding the hold-outs and a targets mapping."""
    src = tmp_path / "src"
    shutil.copytree(BENCH, src, ignore=shutil.ignore_patterns("holdout_v2", "driftspec_v2.json", "__pycache__",
                                                              ".pytest_cache", "*.egg-info"))
    (src / ".gitignore").write_text("__pycache__/\n")
    git(src, "init", "-q", "-b", "main")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "init")
    bare = tmp_path / "fleet-target.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))

    home = tmp_path / "home"
    shutil.copytree(BENCH / "holdout_v2", home / "benchmarks" / "bench" / "holdout_v2",
                    ignore=shutil.ignore_patterns("__pycache__"))
    mapping = home / "targets.json"
    mapping.write_text(json.dumps({"fleet-target": {"holdout": "benchmarks/bench/holdout_v2"}}))
    return f"file://{bare}", bare, home, mapping


def test_url_helpers():
    assert target.is_url("https://github.com/ddavarma/fleet-telemetry-v1")
    assert target.is_url("file:///tmp/x.git") and not target.is_url("benchmarks/robotics_telemetry_drift")
    assert target.slug_from_url("https://github.com/ddavarma/fleet-telemetry-v1") == "ddavarma/fleet-telemetry-v1"
    assert target.slug_from_url("https://github.com/ddavarma/fleet-telemetry-v1.git/") == "ddavarma/fleet-telemetry-v1"
    assert target.slug_from_url("git@github.com:ddavarma/fleet-telemetry-v1.git") == "ddavarma/fleet-telemetry-v1"
    assert target.slug_from_url("file:///tmp/a/fleet-target.git") == "fleet-target"


def test_real_mapping_points_at_the_benchmark_holdouts():
    t = target.Target(path=Path("/nowhere"), url="https://github.com/ddavarma/fleet-telemetry-v1",
                      slug="ddavarma/fleet-telemetry-v1")
    assert target.holdout_for(t, ROOT) == BENCH / "holdout_v2"
    assert target.holdout_for(target.Target(path=Path("/x"), slug="someone/else"), ROOT) is None


def test_clone_worktree_verify_and_push_to_target(remote):
    url, bare, home, mapping = remote
    t = target.prepare(url, home, "u1")
    assert t.clone == home / ".driftzero" / "clones" / "u1" and t.slug == "fleet-target"
    wt = repair.create_worktree(t.path, "u1", home=home)
    try:
        assert wt.path == home / ".driftzero" / "wt-u1" and wt.project_rel == "." and wt.root == t.clone
        assert wt.runs_dir == home / ".driftzero" / "runs"
        git(wt.path, "apply", "-p3", str(MUSE_DIFF))
        holdout = target.holdout_for(t, home, targets_file=mapping)
        v = verify.verify_local(wt.path, holdout, project_rel=wt.project_rel)
        assert (v.agent.passed_count, v.holdout.passed_count, v.holdout.tests) == (21, 7, 7) and v.passed

        spec = DriftSpec.load(BENCH / "driftspec_v2.json")
        g = guard_check(wt, repair.collect_diff(wt))
        wt.runs_dir.mkdir(parents=True, exist_ok=True)
        res = loop.LoopResult(task_id="u1", worktree=wt, spec=spec, blast_radius=analyze(t.path, spec), passed=True,
                              iterations=[{"iteration": 1, "guard": g.as_dict(), "repair": {"duration_s": 1.0},
                                           "verify": {"wall_time_s": 0.5}}],
                              log_path=wt.runs_dir / "u1.jsonl", cost=Cost(), wall_time_s=2.0, model="muse-spark-1.3",
                              steps=1, guard=g, verify=v, target_url=url, target_slug=t.slug,
                              diff_cost=Cost(2858, 0, 5085, 0, 0.0252),
                              command=f"driftzero migrate --repo {url} --changelog C.md")
        out = pr.open_pr(res, create=False)
        assert git(bare, "rev-parse", f"refs/heads/{wt.branch}").strip() == out.commit
        assert pr.title(res) == "DriftZero: migrate fleet-target to Fleet Telemetry API v2"
        body = pr.render_body(res)
        assert "| of which DriftSpec generation | $0.0252" in body
        assert body.split("## Reproduce")[1].strip().splitlines()[0] == f"`driftzero migrate --repo {url} --changelog C.md`"
    finally:
        repair.remove_worktree(wt)


@needs_muse
def test_loop_dry_run_on_url_target(remote):
    url, _, home, mapping = remote
    res = loop.repair_until_verified(url, BENCH / "driftspec_v2.json", max_iterations=1, dry_run=True, local=True,
                                     home=home, targets_file=mapping, on_line=lambda s: None)
    try:
        start = json.loads(res.log_path.read_text().splitlines()[0])
        assert start["target_url"] == url and start["target_slug"] == "fleet-target"
        assert res.worktree.root == home / ".driftzero" / "clones" / res.task_id
        assert res.worktree.path == home / ".driftzero" / f"wt-{res.task_id}"
        it = res.iterations[0]
        assert it["repair"]["terminal"] == "completed"  # Muse accepts a worktree outside the source repo
        assert it["verify"]["agent"]["passed"] == 18 and it["verify"]["holdout"] == {
            "exit_code": 1, "tests": 7, "passed": 2, "failed": it["verify"]["holdout"]["failed"]}
        assert not res.passed
    finally:
        repair.remove_worktree(res.worktree)
