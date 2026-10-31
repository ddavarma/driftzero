"""driftzero watch: log parsing, state transitions, live tailing, layout. Uses the committed run logs in docs/runs."""
import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from driftzero import repair as repair_mod
from driftzero import watch
from driftzero.cli import app

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "docs" / "runs"
PR4 = "20261030-155223-26855b"        # fleet-telemetry-v1 PR #4: 2 iterations, review approve, no fix pass
JOBTRACKER = "20261030-171945-fe1c00"  # JobTracker PR #1
FIX = "20261030-161858-a70fa1"         # fleet-telemetry-v1 PR #5: review-fix pass, second review
NOT_VERIFIED = "20261030-165401-ebb8d2"
CRASHED = "20261030-231611-936398"     # guard crashed after Muse finished: no iteration record


def records(run_id):
    return [json.loads(line) for line in (RUNS / f"{run_id}.jsonl").read_text().splitlines()]


def final_state(run_id):
    state = watch.WatchState(run_id=run_id)
    ticks = watch.replay_ticks(RUNS, run_id)
    for tk in ticks:
        state.apply(*tk)
    if not state.done:
        state.apply(ticks[-1][0], "exit", "log ended")
    return state


def statuses(state):
    return {n: st.status for n, st in state.stages.items()}


@pytest.mark.parametrize("muse_file", sorted(RUNS.glob("*.muse-i*.jsonl")), ids=lambda p: p.name)
def test_muse_view_counts_steps_like_the_pipeline(muse_file):
    trace, view = repair_mod.MuseTrace(), watch.MuseView()
    for line in muse_file.read_text().splitlines():
        event = trace.feed_line(line)
        if event:
            view.feed(event)
    assert (view.steps, view.tool_calls) == (trace.steps, len(trace.commands))


def test_muse_view_lines():
    view = watch.MuseView()
    lines = []
    for line in (RUNS / f"{JOBTRACKER}.muse-i1.jsonl").read_text().splitlines():
        lines += view.feed(json.loads(line))
    assert lines[0] == "step  1  model call"
    assert any(ln.startswith("    $ ") and ln.endswith("[exit 0]") for ln in lines)
    assert any(re.match(r"    edit_file .*\(\+\d+ -\d+\)$", ln) for ln in lines)  # files written, with counts
    assert any(ln.startswith("    ! tool.bash failed") for ln in lines)
    assert lines[-1] == "muse completed: 23 model steps, 24 tool calls"


def test_short_command_drops_the_run_venv_path():
    cmd = "cd . && /x/.driftzero/venvs/20261030-1-abc/bin/python -m pytest -q\n   tests/"
    assert watch.short_command(cmd) == "python -m pytest -q tests/"
    assert watch.short_command("/Users/x/proj/.venv/bin/python -m pytest") == "python -m pytest"
    assert watch.short_command("cat /etc/bin/notes") == "cat /etc/bin/notes"


def test_replay_ticks_are_ordered_and_complete():
    ticks = watch.replay_ticks(RUNS, PR4)
    assert [t for t, *_ in ticks] == sorted(t for t, *_ in ticks)
    kinds = [k for _, k, _ in ticks]
    assert kinds.count("muse_start") == 2 and kinds.count("verify") == 2 and kinds.count("repair_done") == 2
    run_events = [p["event"] for _, k, p in ticks if k == "run"]
    assert run_events == [r["event"] for r in records(PR4)]
    # iteration 2 starts after iteration 1's record, and Muse's steps sit inside each repair's logged duration
    i = {r["iteration"]: r for r in records(PR4) if r["event"] == "iteration"}
    s2 = next(t for t, k, p in ticks if k == "muse_start" and p == 2)
    assert s2 >= i[1]["ts"]
    muse1 = [t for t, k, p in ticks if k == "muse" and p[0] == 1]
    start1 = i[1]["ts"] - i[1]["wall_time_s"]
    assert start1 <= min(muse1) and max(muse1) <= start1 + i[1]["repair"]["duration_s"] + 0.01


def test_pr4_final_state_matches_its_logs():
    state = final_state(PR4)
    recs = records(PR4)
    its = [r for r in recs if r["event"] == "iteration"]
    assert statuses(state) == {"differ": "ok", "blast radius": "ok", "repair": "ok", "guard": "ok", "verify": "ok",
                               "PR": "ok", "review": "ok", "review-fix": "skipped"}
    assert state.steps == sum(r["repair"]["steps"] for r in its) == 28
    assert state.tool_calls == sum(r["repair"]["tool_calls"] for r in its)
    assert (state.iteration, state.verified, state.target) == (2, True, "ddavarma/fleet-telemetry-v1")
    v = state.verify
    assert (v.iteration, v.agent.passed, v.agent.tests, v.holdout.passed, v.holdout.tests) == (2, 22, 22, 8, 8)
    assert ("test_health_outside_documented_range_is_rejected", "passed") in v.holdout.cases
    assert state.guard == {"iteration": 2, "ok": True, "rejections": 0, "review_items": 13, "fix": False}
    assert state.review["verdict"] == "approve" and state.posted == "COMMENT"
    assert state.stages["PR"].note == "PR #4"
    end, review = recs[[r["event"] for r in recs].index("end")], next(r for r in recs if r["event"] == "review")
    assert state.totals()["usd"] == pytest.approx(end["cost"]["usd"] + review["cost"]["usd"], abs=1e-3)
    assert state.totals()["input_tokens"] == end["cost"]["input_tokens"] + review["cost"]["input_tokens"]
    # verify ran twice; its elapsed is the two sandbox wall times, not the repairs
    assert state.stages["verify"].elapsed(0) == pytest.approx(sum(r["verify"]["wall_time_s"] for r in its), abs=0.2)
    assert state.stages["differ"].elapsed(0) == pytest.approx(recs[0]["latency_s"], abs=0.01)


def test_pr4_mid_replay_shows_the_failed_holdout_of_iteration_1():
    state = watch.WatchState(run_id=PR4)
    for tk in watch.replay_ticks(RUNS, PR4):
        state.apply(*tk)
        if tk[1] == "muse_start" and tk[2] == 2:
            break
    assert state.verify.iteration == 1 and (state.verify.holdout.passed, state.verify.holdout.tests) == (7, 8)
    assert ("test_health_outside_documented_range_is_rejected", "failed") in state.verify.holdout.cases
    assert state.stages["repair"].status == "running" and state.stages["verify"].status == "pending"
    assert state.stages["PR"].status == "pending"
    assert state.stages["verify"].note == "iteration 1: failed" and state.stages["guard"].note == "iteration 1: ok"


def test_jobtracker_final_state():
    state = final_state(JOBTRACKER)
    assert state.steps == 23 and state.stages["differ"].note == "changelog only"
    assert (state.verify.agent.passed, state.verify.holdout.passed, state.verify.holdout.tests) == (5, 7, 7)
    assert [f["confidence"] for f in state.review["findings"]] == [0.35]
    assert state.stages["review-fix"].status == "skipped"
    assert round(state.totals()["usd"], 4) == 0.3953  # $0.3694 loop + $0.0259 review


def test_review_fix_run_and_second_review():
    state = final_state(FIX)
    assert state.stages["review-fix"].status == "ok" and state.stages["review-fix"].note == "verified, pushed"
    assert state.fix_iteration == 2 and state.review["second"] is True
    assert state.stages["repair"].note.startswith("iteration 1")  # the fix pass is not a loop iteration


def test_not_verified_and_crashed_runs():
    s = final_state(NOT_VERIFIED)
    assert s.verified is False and s.stages["verify"].status == "failed"
    assert {s.stages[n].status for n in ("PR", "review", "review-fix")} == {"skipped"}
    c = final_state(CRASHED)
    assert c.stages["repair"].status == "ok" and "not logged" in c.stages["repair"].note
    assert c.stages["guard"].status == "failed" and c.stages["verify"].status == "skipped"
    assert c.steps == 21


def test_flags_and_review_fix_trigger():
    f = watch.parse_flags("driftzero migrate --repo x --no-pr --review-fix --review-fix-min-confidence 0.3")
    assert f["no_pr"] and f["review_fix"] and not f["no_review"] and f["min_confidence"] == 0.3
    finding = {"severity": "medium", "category": "other", "file": "a.py", "line_hint": "1", "evidence": "",
               "explanation": "", "suggested_fix": "", "confidence": 0.7}
    assert watch.review_fix_triggers({"verdict": "comment", "findings": [finding]}, watch.parse_flags(""))
    assert not watch.review_fix_triggers({"verdict": "comment", "findings": [{**finding, "confidence": 0.5}]},
                                         watch.parse_flags(""))


def test_no_pr_no_review_run_completes_after_the_push():
    s = watch.WatchState()
    start = {**records(PR4)[1], "command": "driftzero migrate --repo x --no-review --no-pr"}
    s.apply(1.0, "run", start)
    s.apply(2.0, "run", {"event": "end", "passed": True, "cost": {}})
    assert s.stages["PR"].status == "running" and not s.done
    s.apply(3.0, "run", {"event": "pr", "url": None, "pr_s": 1.0})
    assert statuses(s)["PR"] == "skipped" and s.stages["PR"].note == "branch pushed, --no-pr"
    assert statuses(s)["review"] == "skipped" and s.done and s.wall(99.0) == 2.0


def test_process_exit_closes_open_stages():
    s = watch.WatchState()
    s.apply(1.0, "mark", ("differ", "running", ""))
    s.apply(5.0, "exit", "process exited")
    assert statuses(s)["differ"] == "failed" and statuses(s)["PR"] == "skipped" and s.done


def _copy(src_id, dst: Path, suffix: str, rid: str, mtime: float):
    p = dst / f"{rid}{suffix}"
    shutil.copy(RUNS / f"{src_id}{suffix}", p)
    os.utime(p, (mtime, mtime))
    return p


def test_live_tailer_follows_a_run_as_its_files_appear(tmp_path):
    """Feed PR #4's files into a fresh .driftzero/ in the order the pipeline writes them."""
    state_dir = tmp_path / ".driftzero"
    runs = state_dir / "runs"
    runs.mkdir(parents=True)
    recs = records(PR4)
    rid = "20991231-235959-abcdef"
    since = time.time()
    tailer = watch.LiveTailer(state_dir, None, since=since)
    state = watch.WatchState()

    def poll():
        for tk in tailer.poll():
            state.apply(*tk)

    poll()
    assert state.stages["differ"].status == "running" and state.run_id is None
    (state_dir / f"wt-{rid}").mkdir()
    log = runs / f"{rid}.jsonl"
    log.write_text("".join(json.dumps({**r, "task_id": rid}) + "\n" for r in recs[:2]))  # diff, start
    poll()
    assert state.run_id == rid and statuses(state)["differ"] == "ok" and statuses(state)["blast radius"] == "ok"
    assert statuses(state)["repair"] == "running"

    _copy(PR4, runs, ".prompt-i1.md", rid, recs[1]["ts"])
    muse_lines = (RUNS / f"{PR4}.muse-i1.jsonl").read_text().splitlines(keepends=True)
    half = len(muse_lines) // 2
    (runs / f"{rid}.muse-i1.jsonl").write_text("".join(muse_lines[:half]) + muse_lines[half][:20])  # partial line
    poll()
    mid = state.muse[1].steps
    assert 0 < mid < 17 and state.iteration == 1
    with (runs / f"{rid}.muse-i1.jsonl").open("a") as fh:
        fh.write(muse_lines[half][20:] + "".join(muse_lines[half + 1:]))
    poll()
    assert state.muse[1].steps == 17  # the partial line was read once, whole

    it1 = recs[2]
    _copy(PR4, runs, ".i1.diff", rid, it1["ts"] - it1["wall_time_s"] + it1["repair"]["duration_s"])
    poll()
    assert statuses(state)["repair"] == "ok" and statuses(state)["guard"] == "running"
    _copy(PR4, runs, ".i1.verify.json", rid, it1["ts"])
    poll()
    assert statuses(state)["guard"] == "ok" and statuses(state)["verify"] == "failed"
    assert state.stages["verify"].elapsed(0) == pytest.approx(4.5, abs=0.01)  # back-dated by the sandbox wall time
    with log.open("a") as fh:
        fh.write(json.dumps({**it1, "task_id": rid}) + "\n")
    poll()
    assert state.guard["review_items"] == 13 and "retrying" in state.stages["repair"].note
    _copy(PR4, runs, ".prompt-i2.md", rid, it1["ts"] + 0.1)
    _copy(PR4, runs, ".muse-i2.jsonl", rid, it1["ts"] + 30)
    poll()
    assert state.iteration == 2 and statuses(state)["repair"] == "running" and statuses(state)["verify"] == "pending"
    assert state.steps == 28


def test_live_tailer_ignores_runs_from_before_since(tmp_path):
    state_dir = tmp_path / ".driftzero"
    (state_dir / "runs").mkdir(parents=True)
    (state_dir / "wt-20200101-000000-aaaaaa").mkdir()
    tailer = watch.LiveTailer(state_dir, None, since=time.time())
    assert all(k != "discovered" for _, k, _ in tailer.poll())


EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️]")


def test_layout_is_120_columns_dark_and_emoji_free():
    state = final_state(PR4)
    console = Console(width=120, height=watch.HEIGHT, record=True, file=open(os.devnull, "w"), force_terminal=True,
                      color_system="truecolor")
    console.print(watch.render(state, 0.0))
    text = console.export_text(clear=False)
    lines = text.splitlines()
    assert len(lines) == watch.HEIGHT and all(len(ln) <= 120 for ln in lines)
    assert max(len(ln) for ln in lines) == 120
    for stage in watch.STAGES:
        assert stage in text
    for needle in ("run 20261030-155223-26855b", "iteration 2/3", "step 11", "total 28", "agent suite 22/22",
                   "hold-outs   8/8", "client_requests_v2_paths", "13 review items", "approve", "$0.6844", "88.7%"):
        assert needle in text, needle
    assert not EMOJI.search(text)
    assert watch.BG.lstrip("#") in console.export_svg().lower()  # background painted, not left to the terminal


def test_watch_cli_replay(tmp_path):
    out = tmp_path / "frame.txt"
    r = CliRunner().invoke(app, ["watch", JOBTRACKER, "--replay", "100000", "--save", str(out), "--home", str(ROOT)])
    assert r.exit_code == 0, r.output
    assert "hold-outs   7/7" in out.read_text()
    r = CliRunner().invoke(app, ["watch", "--replay", "10"])
    assert r.exit_code != 0
