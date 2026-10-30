import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from driftzero import repair
from driftzero.blast_radius import analyze
from driftzero.cli import app
from driftzero.spec import DriftSpec

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "robotics_telemetry_drift"
SPEC_PATH = BENCH / "driftspec_v2.json"
SMOKE = ROOT / "docs" / "muse_smoke_full.jsonl"

needs_muse = pytest.mark.skipif(shutil.which("muse") is None, reason="muse CLI not on PATH")


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def mono_repo(tmp_path):
    """A git repo with the benchmark in a subdirectory, like the real layout."""
    root = tmp_path / "repo"
    shutil.copytree(BENCH, root / "bench", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info"))
    (root / ".gitignore").write_text(".driftzero/\n__pycache__/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------- event parsing on the real capture


def test_trace_parses_real_smoke_capture():
    lines = []
    trace = repair.MuseTrace(on_line=lines.append)
    for line in SMOKE.read_text().splitlines():
        trace.feed_line(line)
    assert trace.model == "muse-spark-1.3"
    assert trace.steps == 2
    assert [(c.command, c.exit_code, c.tool) for c in trace.commands] == [
        ("ls -R benchmarks/robotics_telemetry_drift/fleet_telemetry", 0, "bash")]
    assert trace.terminal == "completed"
    assert trace.final_text.startswith("Python files under")
    assert trace.token_totals()["input_tokens"] == 60974
    assert lines[0] == "[step  1] model"
    assert any("$ ls -R" in ln and "exit 0" in ln for ln in lines)


def test_trace_reads_schema_answer_and_ignores_reminder_failures():
    answer = {"files_changed": ["a.py"], "summary": "s", "tests_run": 18, "tests_passed": 18, "confidence": 0.9}
    trace = repair.MuseTrace()
    trace.feed({"payload_type": "task.lifecycle.proposed",
                "payload": {"event": {"task_id": "r", "task_kind": "reminder.agent.goal-reminder"}}})
    trace.feed({"payload_type": "task.lifecycle.failed", "payload": {"event": {"task_id": "r", "reason": "x"}}})
    trace.feed({"payload_type": "run.terminal.completed",
                "payload": {"terminal": "completed", "text": json.dumps(answer), "reason": None}})
    assert trace.failures == [] and trace.steps == 0
    assert repair.RepairAnswer.model_validate_json(trace.final_text).tests_passed == 18


def test_schema_file_matches_answer_model():
    schema = json.loads(repair.SCHEMA_PATH.read_text())
    assert set(schema["required"]) == set(repair.RepairAnswer.model_fields)
    assert schema["additionalProperties"] is False


# ---------------------------------------------------------------- worktree, diff, prompt


def test_worktree_diff_and_cleanup(mono_repo):
    wt = repair.create_worktree(mono_repo / "bench", "t1")
    assert wt.path == mono_repo / ".driftzero" / "wt-t1"
    assert wt.branch == "driftzero/t1" and wt.project_rel == "bench"
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "driftzero/t1"

    (wt.project / "fleet_telemetry" / "client.py").write_text("# changed\n")
    (wt.project / "NEW.md").write_text("new\nfile\n")
    git(wt.path, "commit", "-q", "-am", "committed change")
    (wt.project / "fleet_telemetry" / "models.py").write_text("# uncommitted\n")

    diff = repair.collect_diff(wt)
    paths = {p for p, _, _ in diff.files}
    assert paths == {"bench/fleet_telemetry/client.py", "bench/fleet_telemetry/models.py", "bench/NEW.md"}
    assert "+new" in diff.patch and diff.added >= 4
    # the source checkout is untouched
    assert "# changed" not in (mono_repo / "bench" / "fleet_telemetry" / "client.py").read_text()

    repair.remove_worktree(wt)
    assert not wt.path.exists()
    assert "driftzero/t1" not in git(mono_repo, "branch", "--list")


def test_prompt_contains_rules_fields_changelog_and_retry_traceback(tmp_path):
    spec = DriftSpec.load(SPEC_PATH)
    br = analyze(BENCH, spec)
    fleet_rules = json.loads((ROOT / "benchmarks" / "targets.json").read_text())[
        "benchmarks/robotics_telemetry_drift"]["repair_rules"]
    p = repair.write_repair_prompt(spec, br, project_rel="benchmarks/robotics_telemetry_drift",
                                   out_path=tmp_path / "p.md", repair_rules=fleet_rules)
    text = p.read_text()
    for needle in ("Never delete, skip, xfail", "MIN_HEALTH = 0.5", "Negative `qw`", "sqrt(trace(P_pos) / 3)",
                   "-m pytest -q", "`TelemetryFrame.health`", "`TelemetryFrame.orientation.qw`",
                   "`fleet_telemetry/alerts.py`", "check_frame", "tests/test_fusion.py::test_equal_noise_gives_plain_mean",
                   "yaw = atan2(2(qw·qz + qx·qy)", "Work only inside `benchmarks/robotics_telemetry_drift/`"):
        assert needle in text, needle
    assert "Previous attempt failed" not in text

    tb = "\n".join(f"line {i}" for i in range(500)) + "\nE   AssertionError: tilt"
    retry = repair.write_repair_prompt(spec, br, prior_failure=tb, out_path=tmp_path / "r.md").read_text()
    assert "Previous attempt failed" in retry and "E   AssertionError: tilt" in retry
    assert "line 0\n" not in retry  # traceback is truncated to its tail


def test_muse_argv_live_vs_dry(tmp_path):
    wt = repair.Worktree(root=Path("/r"), path=Path("/r/.driftzero/wt-x"), branch="driftzero/x", base="abc",
                         project_rel=".")
    live = repair.muse_argv(wt, Path("/p.md"), Path("/s.json"))
    assert live[live.index("--workspace") + 1] == "/r"
    assert live[live.index("--worktree-existing") + 1] == "/r/.driftzero/wt-x"
    assert live[live.index("--output-schema") + 1] == "/s.json"
    assert live[live.index("--contributor-mode") + 1] == "off"
    for flag in ("--approval-mode", "--trust-workspace", "--disable-reminders", "--max-model-steps"):
        assert flag in live
    dry = repair.muse_argv(wt, Path("/p.md"), Path("/s.json"), dry_run=True)
    assert dry[dry.index("--provider") + 1] == "echo"
    assert "--output-schema" not in dry and "--model" not in dry
    assert repair.muse_argv(wt, Path("/p"), Path("/s"), contributor=True)[live.index("--contributor-mode") + 1] == "on"


# ---------------------------------------------------------------- end-to-end plumbing via Muse's echo provider (free)


@needs_muse
def test_cli_repair_dry_run_end_to_end(mono_repo):
    res = CliRunner().invoke(app, ["repair", "--repo", str(mono_repo / "bench"), "--spec", str(SPEC_PATH), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "[step  1] model" in res.output
    assert "muse completed (exit 0) in 1 model steps" in res.output
    assert "(no changes)" in res.output

    runs = mono_repo / ".driftzero" / "runs"
    (events,) = runs.glob("*.jsonl")
    types = [json.loads(line)["payload_type"] for line in events.read_text().splitlines()]
    assert "run.lifecycle.started" in types and types.count("run.terminal.completed") == 1
    summary = json.loads(next(runs.glob("*.summary.json")).read_text())
    assert summary["dry_run"] is True and summary["terminal"] == "completed" and summary["answer"] is None
    # the echo provider echoes the prompt we wrote
    assert "Migrate this project from Fleet Telemetry API v1 to v2" in next(runs.glob("*.prompt.md")).read_text()
    # dry runs clean up the worktree and branch
    assert not list((mono_repo / ".driftzero").glob("wt-*"))
    assert "driftzero/" not in git(mono_repo, "branch", "--list")
