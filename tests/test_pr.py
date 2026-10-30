import json
import shutil
import subprocess
from pathlib import Path

import pytest
from types import SimpleNamespace

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
                     iterations=[{"iteration": 1, "guard": g.as_dict(), "repair": {"duration_s": 150.0},
                                  "verify": {"wall_time_s": v.wall_time_s}}], log_path=root / ".driftzero" / "runs" / "p1.jsonl",
                     cost=Cost(1393249, 1279572, 30026, 19186, 0.4616), wall_time_s=160.0, model="muse-spark-1.3",
                     steps=22, answer=answer, guard=g, verify=v, hidden_holdout_files=hidden)
    yield res, origin
    repair.remove_worktree(res.worktree)


def test_body_has_every_section(verified):
    res, _ = verified
    body = pr.render_body(res, pr_s=3.2, spec_path=str(res.worktree.root / BENCH_REL / "driftspec_v2.json"))
    lead = body.splitlines()[:3]
    assert lead[0].startswith("> **Changed** ") and "+" in lead[0] and "library" in lead[0]
    assert lead[1].startswith("> **Verified** agent suite 21/21 · hold-out 8/8")
    assert lead[2].startswith("> **Cost** $0.4616 · 160 s repair loop + 3.2 s PR")
    headings = [ln for ln in body.splitlines() if ln.startswith("## ")]
    assert headings == ["## Agent's own summary", "## Review findings", "## API change (DriftSpec)", "## Blast radius",
                        "## Test changes, review required", "## Verification", "## Cost", "## Logs", "## Reproduce"]
    assert "Migrated to quaternions and covariance." in body
    assert "| field removed | `TelemetryFrame.noise` |" in body
    assert "**check_frame**" in body and "summarize" in body


def test_review_findings_and_collapsed_added_tests(verified):
    res, _ = verified
    body = pr.render_body(res)
    findings = body.split("## Review findings")[1].split("## API change")[0]
    assert "| source | severity (confidence) | kind | where | finding |" in findings
    assert "| guard (static) | – | stale test name | `" in findings and "test_client_hits_v1_paths" in findings
    changes = body.split("## Test changes, review required")[1].split("## Verification")[0]
    assert "`['/v1/robots/amr-03/telemetry/latest', '/v1/fleet/telemetry']` → `['/v2/robots" in changes
    assert "| test data |" in changes
    assert "test added" not in changes and changes.count("**Added:** 3 new tests in ") == 1
    added = [f for f in res.guard.review_items if f.rule == "added_test"]
    assert f"({sum(int(f.new) for f in added)} asserts total)" in changes


def test_blast_radius_coverage_from_link_graph(verified):
    res, _ = verified
    cov = pr.coverage(res.blast_radius)
    # every test parses through conftest fixtures -> client.parse_batch; charging is not in the blast radius at all
    assert len(cov["fleet_telemetry/models.py"]) == 16
    assert cov["fleet_telemetry/report.py"] == {"tests/test_alerts.py::test_fleet_summary"}
    assert "tests/test_client.py" not in cov
    body = pr.render_body(res)
    assert "| file | impacted functions / classes | covered by tests |" in body
    assert "| `fleet_telemetry/report.py` | summarize | 1 |" in body
    assert "| `tests/test_client.py` | – | 4 (tests in this file) |" in body


def test_verification_lists_holdouts_logs_and_reproduce(verified):
    res, _ = verified
    body = pr.render_body(res, pr_s=3.2, spec_path=str(res.worktree.root / BENCH_REL / "driftspec_v2.json"),
                          logs=("https://github.com/o/r/blob/main", "main", [f"docs/runs/{res.task_id}.jsonl"]))
    for name in ("test_client_requests_v2_paths", "test_negative_qw_robots_heading_and_tilt",
                 "test_charging_schedule_unaffected"):
        assert f"- ✅ `{name}` passed" in body
    assert body.count("- ✅ `") == 8
    assert "**Wall time:** repair " in body and " · PR 3.2 s · total 163 s" in body
    assert f"- [`{res.task_id}.jsonl`](https://github.com/o/r/blob/main/docs/runs/{res.task_id}.jsonl) · loop log" in body
    assert body.count("<details>") == 2
    repro = body.split("## Reproduce")[1].strip().splitlines()[0]
    assert repro == (f"`git checkout {res.worktree.base[:7]} && driftzero migrate --repo {BENCH_REL} "
                     f"--spec {BENCH_REL}/driftspec_v2.json`")
    no_logs = pr.render_body(res)
    assert f"Run files are local at `.driftzero/runs/{res.task_id}.*`" in no_logs and "PR n/a" in no_logs


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


# ---------------------------------------------------------------- --review-fix

from driftzero import loop as loop_mod  # noqa: E402
from driftzero import reviewer as reviewer_mod  # noqa: E402

FINDING = reviewer_mod.Finding(severity="medium", category="test_shaped_code",
                               file="benchmarks/robotics_telemetry_drift/fleet_telemetry/client.py", line_hint="50",
                               evidence="+        x = round(y, 3)", explanation="Shaped by a test.",
                               suggested_fix="Keep full precision.")


def first_review():
    return reviewer_mod.ReviewOutcome(result=reviewer_mod.ReviewResult(verdict="comment", findings=[FINDING]),
                                      provider="baseten", model="meta-models/muse-spark-open-1026", effort="high",
                                      cost=Cost(1, 0, 1, 0, 0.03), latency_s=20, attempts=1)


def test_review_fix_prompt_lists_findings_verbatim(verified, tmp_path):
    res, _ = verified
    p = repair.write_repair_prompt(res.spec, res.blast_radius, reviewer_findings=[FINDING], out_path=tmp_path / "p.md")
    text = p.read_text()
    assert "## Reviewer findings to address or explicitly justify" in text
    assert "### Finding 1: medium · test_shaped_code · `benchmarks/robotics_telemetry_drift/fleet_telemetry/client.py`" in text
    assert "+        x = round(y, 3)" in text and "Shaped by a test." in text and "Suggested fix: Keep full precision." in text
    assert "never delete or weaken a test" in text and "`reviewer_responses`" in text
    plain = repair.write_repair_prompt(res.spec, res.blast_radius, out_path=tmp_path / "q.md").read_text()
    assert "Reviewer findings" not in plain


def test_failed_review_fix_is_reverted_and_reported(verified, monkeypatch):
    res, _ = verified
    pr.open_pr(res, create=False)
    client_py = res.worktree.project / "fleet_telemetry" / "client.py"
    before = client_py.read_text()

    def failing_iteration(wt, spec, br, n, of, **kw):
        client_py.write_text("broken = True\n")
        rec = {"iteration": n, "kind": "review_fix", "passed": False, "wall_time_s": 1.0,
               "repair": {"steps": 2, "duration_s": 1.0}, "cost": {}, "diff": {}, "verify": None,
               "guard": {"ok": True, "rejections": [], "review_items": []}}
        run = SimpleNamespace(steps=2, answer=None)
        return rec, run, Cost(10, 0, 5, 0, 0.01), None, None, None

    monkeypatch.setattr(loop_mod, "run_iteration", failing_iteration)
    res.settings = {"holdout": None, "local": True}
    info = pr.apply_review_fix(res, "https://github.com/o/r/pull/9", first_review(), on_line=lambda s: None)
    assert info["reverted"] and not info["verified"] and info["commit"] is None and info["second_review"] is None
    assert client_py.read_text() == before  # PR branch content untouched
    body = pr.render_body(res)
    section = body.split("## Reviewer-driven fixes")[1].split("## API change")[0]
    assert "| 1 | medium · test_shaped_code" in section and "_no response given_" in section
    assert "did not verify (verification failed)" in section and "still contains the first, verified version" in section


def test_verified_review_fix_commits_pushes_and_rereviews(verified, monkeypatch):
    res, origin = verified
    first_sha = pr.open_pr(res, create=False).commit
    res.target_slug = "o/r"
    client_py = res.worktree.project / "fleet_telemetry" / "client.py"

    def fixing_iteration(wt, spec, br, n, of, **kw):
        client_py.write_text(client_py.read_text() + "\n# reviewed\n")
        answer = repair.RepairAnswer(files_changed=[str(client_py)], summary="s", tests_run=21, tests_passed=21,
                                     confidence=0.9, reviewer_responses=[{"finding": 1, "action": "changed",
                                                                          "detail": "dropped the rounding"}])
        rec = {"iteration": n, "kind": "review_fix", "passed": True, "wall_time_s": 3.0,
               "repair": {"steps": 4, "duration_s": 2.0}, "cost": {}, "diff": {},
               "verify": {"agent": {"passed": 21, "tests": 21}, "holdout": {"passed": 7, "tests": 7}},
               "guard": {"ok": True, "rejections": [], "review_items": []}}
        return rec, SimpleNamespace(steps=4, answer=answer), Cost(10, 0, 5, 0, 0.02), None, res.guard, res.verify

    def fake_review_pr(r, url, **kw):
        r.review = reviewer_mod.ReviewOutcome(result=reviewer_mod.ReviewResult(verdict="approve", findings=[]),
                                              provider="baseten", model="m", effort="high", cost=Cost(1, 0, 1, 0, 0.02),
                                              latency_s=10, attempts=1)
        return {"event": "COMMENT", "url": url + "#r2", "inline": 0, "body_only": 0}

    monkeypatch.setattr(loop_mod, "run_iteration", fixing_iteration)
    monkeypatch.setattr(pr, "review_pr", fake_review_pr)
    res.settings = {"holdout": None, "local": True}
    info = pr.apply_review_fix(res, "https://github.com/o/r/pull/9", first_review(), on_line=lambda s: None)
    assert info["verified"] and info["prev_commit"] == first_sha and info["commit"] != first_sha
    assert git(origin, "rev-parse", f"refs/heads/{res.worktree.branch}").strip() == info["commit"]
    assert list(info["hunks"]) == [f"{BENCH_REL}/fleet_telemetry/client.py"]
    assert info["second_review"]["verdict"] == "approve"
    body = pr.render_body(res)
    section = body.split("## Reviewer-driven fixes")[1].split("## API change")[0]
    assert "**changed**: dropped the rounding" in section
    assert f"(https://github.com/o/r/commit/{info['commit']})" in section and "`client.py` @@" in section
    assert "agent suite 21/21, hold-out 7/7" in section and "Second review: verdict **approve**, 0 finding(s)" in section
    assert "Muse Spark Open reviewer (pass 2)" in body
    events = [json.loads(line)["event"] for line in res.log_path.read_text().splitlines()]
    assert events[-1] == "review_fix" and "iteration" in events


def _outcome(findings, verdict="comment"):
    return reviewer_mod.ReviewOutcome(result=reviewer_mod.ReviewResult(verdict=verdict, findings=findings),
                                      provider="baseten", model="meta-models/muse-spark-open-1026", effort="high",
                                      cost=Cost(1, 0, 1, 0, 0.03), latency_s=20, attempts=1)


def test_v21_confidence_split_and_policy_verdict(verified):
    res, _ = verified
    sure = FINDING.model_copy(update={"confidence": 0.8})
    unsure = FINDING.model_copy(update={"confidence": 0.3, "category": "precision_loss", "explanation": "Maybe lossy."})
    res.review = _outcome([sure, unsure])
    body = pr.render_body(res)
    findings = body.split("## Review findings")[1].split("## API change")[0]
    assert "| Muse Spark Open reviewer | medium (0.80) | test_shaped_code |" in findings
    low = findings.split("**Possible issues (low confidence).**")[1]
    assert "| 0.30 | medium | precision_loss |" in low and "Maybe lossy." in low
    assert "Maybe lossy." not in findings.split("**Possible issues")[0]
    assert "verdict **comment**, 1 finding(s) with confidence >= 0.5, 1 low-confidence" in findings
    # approve iff nothing reaches 0.5, whatever the model said
    assert _outcome([unsure], verdict="comment").verdict_label == "approve"
    assert _outcome([sure], verdict="approve").verdict_label == "comment"
    # legacy (v1/v2) findings have no confidence and count as confident
    assert _outcome([FINDING]).verdict_label == "comment" and FINDING.conf == 1.0


def test_v21_fix_trigger_threshold(verified, monkeypatch):
    res, _ = verified
    mid = FINDING.model_copy(update={"confidence": 0.45})
    low_sev = FINDING.model_copy(update={"confidence": 0.9, "severity": "low"})
    out = _outcome([mid, low_sev])
    assert pr.fix_candidates(out) == [] and pr.fix_candidates(out, 0.3) == [mid]
    pr.open_pr(res, create=False)

    def noop_iteration(wt, spec, br, n, of, **kw):
        rec = {"iteration": n, "kind": "review_fix", "passed": False, "wall_time_s": 1.0,
               "repair": {"steps": 1, "duration_s": 1.0}, "cost": {}, "diff": {}, "verify": None,
               "guard": {"ok": True, "rejections": [], "review_items": []}}
        return rec, SimpleNamespace(steps=1, answer=None), Cost(), None, None, None

    monkeypatch.setattr(loop_mod, "run_iteration", noop_iteration)
    res.settings = {"holdout": None, "local": True}
    info = pr.apply_review_fix(res, "https://github.com/o/r/pull/9", out, on_line=lambda s: None, min_confidence=0.3)
    assert info["min_confidence"] == 0.3 and [f["confidence"] for f in info["findings"]] == [0.45]
    section = pr.render_body(res).split("## Reviewer-driven fixes")[1].split("## API change")[0]
    assert "confidence >= **0.3** (lowered from the default 0.6 by a manual `driftzero review-fix` run" in section
