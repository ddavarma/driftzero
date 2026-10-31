"""`driftzero watch <run-id>`: a live terminal view of a run, built only from the files the run writes.

Display only. Nothing here changes or calls back into the pipeline; it reads `.driftzero/runs/<id>.jsonl`
(differ, start, one `iteration` record per pass, end, pr, review, review_fix), Muse's raw events
(`<id>.muse-i<n>.jsonl`) and the per-iteration files the loop leaves behind (`prompt-i<n>.md` just before
Muse starts, `i<n>.diff` when it stops, `i<n>.verify.json` when the sandbox returns).

Two sources feed one state machine (`WatchState.apply`):
- live (`LiveTailer`): polls those files. Times come from the log's own `ts` and from file mtimes, so a
  watcher started late still shows real elapsed times. The pipeline writes nothing between the guard
  finishing and the sandbox returning, so live, the guard shows as running until `i<n>.verify.json`
  arrives and is then back-dated by the sandbox's measured wall time.
- replay (`replay_ticks`): rebuilds the timeline of a finished run from logged timestamps and durations.
  Muse's own event timestamps are not wall-clock, so replay spaces Muse's steps evenly across the
  repair's recorded duration.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

STAGES = ("differ", "blast radius", "repair", "guard", "verify", "PR", "review", "review-fix")
TASK_ID_RE = re.compile(r"^(\d{8}-\d{6})-[0-9a-f]{6}$")
WIDTH, HEIGHT = 120, 38
LEFT_W, RIGHT_W = 30, 42

# dark theme (GitHub-dark-like); the background is painted explicitly so the terminal theme doesn't matter
BG = "#0d1117"
FG = "#c9d1d9"
DIM = "#6e7681"
BORDER = "#30363d"
ACCENT = "#58a6ff"
STATUS_STYLE = {"pending": DIM, "running": "bold #e3b341", "ok": "bold #3fb950", "failed": "bold #f85149",
                "skipped": "#8b949e"}


# --------------------------------------------------------------------------- state

@dataclass
class Stage:
    name: str
    status: str = "pending"
    note: str = ""
    spans: list = field(default_factory=list)  # [start, end | None], one per time the stage ran

    def begin(self, t: float, note: str | None = None) -> None:
        if self.status != "running":
            self.status = "running"
            self.spans.append([t, None])
        if note is not None:
            self.note = note

    def finish(self, status: str, t: float, note: str | None = None) -> None:
        if self.spans and self.spans[-1][1] is None:
            self.spans[-1][1] = max(t, self.spans[-1][0])
        self.status = status
        if note is not None:
            self.note = note

    def elapsed(self, now: float) -> float | None:
        if not self.spans:
            return None
        return sum((e if e is not None else max(now, s)) - s for s, e in self.spans)


@dataclass
class SuiteView:
    tests: int
    passed: int
    exit_code: int
    cases: list[tuple[str, str]]  # (test name, passed | failed | error | skipped)


@dataclass
class VerifyView:
    iteration: int
    passed: bool
    backend: str
    wall_s: float | None
    install_exit: int | None
    agent: SuiteView
    holdout: SuiteView


class MuseView:
    """One Muse run's events as display lines. Counts model steps the way repair.MuseTrace does."""

    def __init__(self) -> None:
        self.steps = 0
        self.tool_calls = 0
        self.kinds: dict[str, str] = {}
        self.patches: dict[str, str] = {}  # call_id -> "path (+a -r)"
        self.usage: dict | None = None
        self.terminal: str | None = None

    def feed(self, event: dict) -> list[str]:
        ptype = event.get("payload_type", "")
        p = event.get("payload") or {}
        ev = p.get("event") or {}
        if ptype == "task.lifecycle.proposed":
            kind = ev.get("task_kind", "")
            self.kinds[ev.get("task_id", "")] = kind
            if kind.startswith("model."):
                self.steps += 1
                return [f"step {self.steps:>2}  model call"]
        elif ptype == "task.lifecycle.tool_output_ref":
            ref = ev.get("output_ref") or {}
            if ref.get("kind") == "tool_patch" and "-call_" in (ref.get("id") or ""):
                self.patches["call_" + ref["id"].rsplit("-call_", 1)[1]] = _patch_text(ref)
        elif ptype == "task.lifecycle.failed":
            kind = self.kinds.get(ev.get("task_id", ""), "?")
            if not kind.startswith("reminder."):
                return [f"    ! {kind} failed: {ev.get('reason')}"]
        elif ptype == "tool.result":
            self.tool_calls += 1
            facts = p.get("correlation_facts") or {}
            tool = facts.get("tool_name") or "tool"
            cmd = code = None
            try:
                body = json.loads(p.get("text") or "")
                if isinstance(body, dict):
                    cmd, code = body.get("command"), body.get("exit_code")
            except json.JSONDecodeError:
                pass
            if cmd is not None:
                return [f"    $ {short_command(cmd)}  [exit {code}]"]
            patch = self.patches.get(p.get("call_id") or "")
            if patch:
                return [f"    {tool} {patch}"]
            return [f"    {tool} ({facts.get('outcome') or 'done'})"]
        elif ptype == "run.usage":
            self.usage = {"models": p.get("models") or []}
        elif ptype.startswith("run.terminal."):
            self.terminal = p.get("terminal") or ptype.rsplit(".", 1)[-1]
            return [f"muse {self.terminal}: {self.steps} model steps, {self.tool_calls} tool calls"]
        return []


_ABS_BIN = re.compile(r"(?<![\w/])/\S*/bin/(?=python|pytest|pip|uv\b)")


def short_command(cmd: str) -> str:
    """A shell command as one display line: whitespace collapsed, interpreter paths and `cd . &&` dropped."""
    cmd = _ABS_BIN.sub("", " ".join(cmd.split()))
    return re.sub(r"^cd \. && ", "", cmd)


def _patch_text(ref: dict) -> str:
    s = ref.get("patch_summary") or {}
    counts = f"(+{s.get('added', 0)} -{s.get('removed', 0)})"
    try:  # Muse keeps the patch on this machine; when it is there, name the files it wrote
        files = [f["path"] for f in json.loads(Path(ref["path"]).read_text())["files"]]
        names = [re.sub(r"^.*/\.driftzero/wt-[^/]+/", "", f) for f in files]
        return f"{', '.join(names)} {counts}"
    except (OSError, KeyError, TypeError, ValueError):
        return f"{s.get('files', '?')} file(s) {counts}"


@dataclass
class WatchState:
    run_id: str | None = None
    replay_speed: float | None = None
    stages: dict[str, Stage] = field(default_factory=lambda: {n: Stage(n) for n in STAGES})
    target: str = ""
    flags: dict = field(default_factory=dict)
    max_iterations: int | None = None
    model: str | None = None
    contributor: bool = False
    iteration: int = 0
    fix_iteration: int | None = None
    muse: dict[int, MuseView] = field(default_factory=dict)
    lines: deque = field(default_factory=lambda: deque(maxlen=400))
    guard: dict | None = None
    verify: VerifyView | None = None
    review: dict | None = None
    posted: str | None = None
    costs: dict[str, dict] = field(default_factory=dict)
    t0: float | None = None
    t_end: float | None = None
    done: bool = False
    verified: bool | None = None

    # ---- derived

    @property
    def steps(self) -> int:
        return sum(m.steps for m in self.muse.values())

    @property
    def tool_calls(self) -> int:
        return sum(m.tool_calls for m in self.muse.values())

    def totals(self) -> dict:
        tot = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "usd": 0.0}
        for c in self.costs.values():
            for k in ("input_tokens", "cached_tokens", "output_tokens"):
                tot[k] += c.get(k) or 0
            tot["usd"] += c.get("usd") or 0.0
        tot["cached_pct"] = 100.0 * tot["cached_tokens"] / tot["input_tokens"] if tot["input_tokens"] else 0.0
        return tot

    def wall(self, now: float) -> float:
        if self.t0 is None:
            return 0.0
        return max(0.0, (self.t_end if self.t_end is not None else now) - self.t0)

    # ---- transitions

    def _s(self, name: str) -> Stage:
        return self.stages[name]

    def _complete(self, t: float) -> None:
        self.done = True
        self.t_end = t

    def apply(self, t: float, kind: str, payload=None) -> None:
        if self.t0 is None:
            self.t0 = t
        handler = getattr(self, f"_on_{kind}")
        handler(t, payload)

    def _on_mark(self, t: float, p: tuple) -> None:
        name, status, note = p
        st = self._s(name)
        if status == "running":
            if st.status == "pending":
                st.begin(t, note)
        elif st.status in ("pending", "running"):
            if st.status == "pending" and status == "ok":
                st.begin(t)
            st.finish(status, t, note)

    def _on_discovered(self, t: float, run_id: str) -> None:
        self.run_id = run_id

    def _on_worktree(self, t: float, _=None) -> None:
        for name in ("differ",):
            if self._s(name).status == "running":
                self._s(name).finish("ok", t)
        b = self._s("blast radius")
        if b.status == "pending":
            b.begin(t)
        if b.status == "running":
            b.finish("ok", t)
        if self._s("repair").status == "pending":
            self._s("repair").begin(t, "repair environment")

    def _on_run(self, t: float, ev: dict) -> None:
        getattr(self, "_run_" + ev.get("event", "").lower(), lambda t, ev: None)(t, ev)

    def _run_diff(self, t: float, ev: dict) -> None:
        if ev.get("cost"):
            self.costs["diff"] = ev["cost"]
        d = self._s("differ")
        note = f"{ev.get('candidates', 0)} OpenAPI candidates" if ev.get("candidates") else "changelog only"
        if d.status == "pending":
            d.begin(t)
        if d.status == "running":
            d.finish("ok", t, note)
        else:
            d.note = note
        if self._s("blast radius").status == "pending":
            self._s("blast radius").begin(t)

    def _run_start(self, t: float, ev: dict) -> None:
        self.run_id = ev.get("task_id") or self.run_id
        self.target = ev.get("target_slug") or ev.get("repo") or ""
        self.max_iterations = ev.get("max_iterations")
        self.model = ev.get("model")
        self.contributor = bool(ev.get("contributor"))
        self.flags = parse_flags(ev.get("command") or "")
        d = self._s("differ")
        if d.status == "pending":
            d.finish("skipped", t, "spec given")
        elif d.status == "running":
            d.finish("ok", t)
        b = self._s("blast radius")
        if b.status == "pending":
            b.begin(t)
        b.finish("ok", min(t, b.spans[-1][1] or t) if b.spans else t,
                 f"{ev.get('impacted_symbols', 0)} symbols, {ev.get('impacted_tests', 0)} tests")
        env = ev.get("repair_env") or {}
        r = self._s("repair")
        if r.status == "pending":
            r.begin(t)
        if env.get("seconds") is not None:
            r.note = f"repair env {env['seconds']}s"

    def _on_muse_start(self, t: float, n: int) -> None:
        self.iteration = n
        self.muse.setdefault(n, MuseView())
        rv = self._s("review")
        if rv.status in ("ok", "failed"):  # past the first review: this pass is --review-fix
            self.fix_iteration = n
            self._s("review-fix").begin(t, f"iteration {n}")
            self.lines.append(f"-- iteration {n} (review fix) --")
            return
        r = self._s("repair")
        r.begin(t)
        r.note = f"iteration {n}" + (f"/{self.max_iterations}" if self.max_iterations else "")
        if n > 1:
            for name in ("guard", "verify"):
                st = self._s(name)
                if st.status in ("ok", "failed", "skipped"):
                    st.note = f"iteration {n - 1}: {st.status}"
                    st.status = "pending"
        self.lines.append(f"-- iteration {n}" + (f"/{self.max_iterations}" if self.max_iterations else "") + " --")

    def _on_muse(self, t: float, p: tuple) -> None:
        n, event = p
        view = self.muse.setdefault(n, MuseView())
        for line in view.feed(event):
            self.lines.append(line)
        if view.usage and f"i{n}" not in self.costs:
            from driftzero.telemetry import cost_from_usage

            self.costs[f"i{n}"] = cost_from_usage(view.usage, default_model=self.model,
                                                  contributor=self.contributor).as_dict()

    def _on_repair_done(self, t: float, n: int) -> None:
        if n == self.fix_iteration:
            return
        r = self._s("repair")
        if r.status == "running":
            r.finish("ok", t)
        self._s("guard").begin(t)

    def _on_verify(self, t: float, p: tuple) -> None:
        n, res = p
        self.verify = verify_view(n, res)
        if n == self.fix_iteration:
            return
        start = t - (res.wall_time_s or 0.0)
        g = self._s("guard")
        if g.status == "pending":
            g.begin(start)
        if g.status == "running":
            g.finish("ok", start)
        v = self._s("verify")
        v.begin(start)
        v.finish("ok" if res.passed else "failed", t, f"iteration {n}")

    def _run_iteration(self, t: float, rec: dict) -> None:
        n = rec.get("iteration", 0)
        if rec.get("cost"):
            self.costs[f"i{n}"] = rec["cost"]
        fix = rec.get("kind") == "review_fix"
        if fix:
            self.fix_iteration = n
        g = rec.get("guard")
        if g is not None:
            self.guard = {"iteration": n, "ok": g.get("ok"), "rejections": len(g.get("rejections") or []),
                          "review_items": len(g.get("review_items") or []), "fix": fix}
        vr = rec.get("verify")
        if vr and (self.verify is None or self.verify.iteration != n):
            self.verify = verify_view_from_record(n, vr)
        if fix:
            if rec.get("error"):
                self._s("review-fix").finish("failed", t, rec["error"][:60])
            elif not rec.get("passed"):
                self._s("review-fix").note = "not verified; reverted"
            return
        r, gs, vs = self._s("repair"), self._s("guard"), self._s("verify")
        if rec.get("error"):
            r.finish("failed", t, rec["error"])
            gs.finish("skipped", t, "repair failed")
            vs.finish("skipped", t, "repair failed")
            return
        if r.status == "running":
            r.finish("ok", t)
        if gs.status == "pending":
            gs.begin(t)
        gs.finish("ok" if g and g.get("ok") else "failed", t, f"iteration {n}")
        if g and not g.get("ok"):
            vs.finish("skipped", t, "guard rejected")
        elif vr is None:
            vs.finish("skipped", t, "no hold-outs")
        elif vs.status != "ok" and vs.status != "failed":
            if vs.status == "pending":
                vs.begin(t)
            vs.finish("ok" if vr.get("passed") else "failed", t, f"iteration {n}")
        if not rec.get("passed") and self.max_iterations and n < self.max_iterations:
            r.note = f"iteration {n} failed; retrying"

    def _run_end(self, t: float, ev: dict) -> None:
        self.verified = bool(ev.get("passed"))
        for name in ("repair", "guard", "verify"):
            st = self._s(name)
            if st.status == "running":
                st.finish("failed", t, st.note)
        if ev.get("passed"):
            self._s("PR").begin(t)
            return
        why = "run stopped: repair failed" if ev.get("error") else "not verified"
        for name in ("PR", "review", "review-fix"):
            self._s(name).finish("skipped", t, why)
        self._complete(t)

    def _run_pr(self, t: float, ev: dict) -> None:
        url = ev.get("url")
        p = self._s("PR")
        if p.status == "pending":
            p.begin(t)
        if url:
            p.finish("ok", t, "PR #" + url.rstrip("/").rsplit("/", 1)[-1])
        else:
            p.finish("skipped", t, "branch pushed, --no-pr")
        if url and not self.flags.get("no_review"):
            self._s("review").begin(t)
            return
        why = "--no-review" if url else "no PR"
        self._s("review").finish("skipped", t, why)
        self._s("review-fix").finish("skipped", t, why)
        self._complete(t)

    def _run_review(self, t: float, ev: dict) -> None:
        if ev.get("cost"):
            self.costs[f"review@{ev.get('ts', t)}"] = ev["cost"]
        result = ev.get("result") or {}
        second = self._s("review-fix").status == "running"
        self.review = {"verdict": result.get("verdict") or ev.get("status"), "status": ev.get("status"),
                       "findings": result.get("findings") or [], "second": second,
                       "model": ev.get("model"), "provider": ev.get("provider")}
        if second:
            return
        rv = self._s("review")
        if rv.status == "pending":
            rv.begin(t)
        rv.finish("ok" if ev.get("status") == "ok" else "failed", t, self.review["verdict"] or "")
        rf = self._s("review-fix")
        if not self.flags.get("review_fix"):
            rf.finish("skipped", t, "flag off")
            self._complete(t)
        elif not review_fix_triggers(result, self.flags):
            rf.finish("skipped", t, "no finding over the bar")
            self._complete(t)

    def _run_comment(self, t: float, ev: dict) -> None:
        self.posted = ev.get("event")

    _run_approve = _run_request_changes = _run_comment

    def _run_review_fix(self, t: float, ev: dict) -> None:
        ok = bool(ev.get("verified")) and not ev.get("reverted")
        note = "verified, pushed" if ok and ev.get("commit") else "verified, no change" if ok else "reverted"
        self._s("review-fix").finish("ok" if ok else "failed", t, note)
        self._complete(t)

    def _on_exit(self, t: float, why: str = "process exited") -> None:
        """The run's process is gone (or a replay ran out of events): close whatever is still open."""
        for st in self.stages.values():
            if st.status == "running":
                st.finish("failed", t, st.note or why)
            elif st.status == "pending":
                st.finish("skipped", t, st.note)
        self._complete(t)


def parse_flags(command: str) -> dict:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()

    def opt(name: str, default):
        if name in argv and argv.index(name) + 1 < len(argv):
            return argv[argv.index(name) + 1]
        pre = next((a for a in argv if a.startswith(name + "=")), None)
        return pre.split("=", 1)[1] if pre else default

    return {"no_pr": "--no-pr" in argv, "no_review": "--no-review" in argv, "review_fix": "--review-fix" in argv,
            "min_confidence": float(opt("--review-fix-min-confidence", 0.6)),
            "min_severity": opt("--review-fix-min-severity", "medium")}


def review_fix_triggers(result: dict, flags: dict) -> bool:
    """Whether `migrate --review-fix` would run its extra pass on this review (same rule as pr.fix_candidates)."""
    from driftzero.pr import fix_candidates
    from driftzero.reviewer import ReviewResult

    try:
        outcome = SimpleNamespace(result=ReviewResult.model_validate(result))
    except ValueError:
        return False
    return bool(fix_candidates(outcome, flags.get("min_confidence", 0.6), flags.get("min_severity", "medium")))


def verify_view(n: int, res) -> VerifyView:
    def suite(s) -> SuiteView:
        return SuiteView(s.tests, s.passed_count, s.exit_code, s.cases())

    return VerifyView(n, res.passed, res.backend, res.wall_time_s, (res.setup or {}).get("exit_code"),
                      suite(res.agent), suite(res.holdout))


def verify_view_from_record(n: int, vr: dict) -> VerifyView:
    def suite(s: dict) -> SuiteView:
        failed = [(f.rsplit("::", 1)[-1], "failed") for f in s.get("failed") or []]
        return SuiteView(s.get("tests", 0), s.get("passed", 0), s.get("exit_code", 0), failed)

    return VerifyView(n, bool(vr.get("passed")), vr.get("backend", ""), vr.get("wall_time_s"), None,
                      suite(vr.get("agent") or {}), suite(vr.get("holdout") or {}))


# --------------------------------------------------------------------------- reading the run's files

def find_runs_dir(run_id: str, home: Path) -> Path:
    for d in (home / ".driftzero" / "runs", home / "docs" / "runs"):
        if (d / f"{run_id}.jsonl").exists():
            return d
    raise FileNotFoundError(f"no {run_id}.jsonl under {home}/.driftzero/runs or {home}/docs/runs")


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_verify(path: Path):
    from driftzero.verify import VerifyResult

    return VerifyResult.model_validate_json(path.read_text())


def id_time(run_id: str) -> float | None:
    m = TASK_ID_RE.match(run_id or "")
    return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else None


def _stamp_time(spec_path: str | None) -> float | None:
    m = re.search(r"diff-(\d{8}-\d{6})", spec_path or "")
    return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else None


def _visible(event: dict) -> bool:
    pt = event.get("payload_type", "")
    if pt == "task.lifecycle.proposed":
        return (event.get("payload", {}).get("event", {}).get("task_kind") or "").startswith("model.")
    return pt in ("tool.result", "task.lifecycle.failed")


def spread_muse(n: int, events: list[dict], start: float, end: float) -> list[tuple]:
    """Ticks for one Muse run, its visible events (model steps, tool results, failures) evenly spaced in [start, end]."""
    vis = sum(1 for e in events if _visible(e))
    end = max(end, start)
    ticks, k, t = [], 0, start
    for e in events:
        if _visible(e):
            k += 1
            t = start + (end - start) * k / (vis + 1)
        elif e.get("payload_type", "").startswith(("run.usage", "run.terminal.")):
            t = end
        ticks.append((t, "muse", (n, e)))
    return ticks


def _differ_end(diff: dict, runs_dir: Path) -> float:
    """When the differ's model call returned. The differ's own log has it; otherwise use the spec file's stamp,
    which older runs took after the call and newer ones (with the differ log) take before it."""
    stamp = re.search(r"diff-(\d{8}-\d{6})", diff.get("spec_path") or "")
    log = runs_dir / f"diff-{stamp.group(1)}.jsonl" if stamp else None
    if log and log.exists():
        calls = read_jsonl(log)
        if calls and calls[-1].get("ts"):
            return calls[-1]["ts"]
    t = _stamp_time(diff.get("spec_path"))
    if t is None:
        return diff["ts"]
    lat = diff.get("latency_s") or 0.0
    return t + lat if t + lat <= diff["ts"] + 1 else t


def replay_ticks(runs_dir: Path, run_id: str) -> list[tuple]:
    """The timeline of a finished run as (t, kind, payload) ticks, in the order the run produced them."""
    events = read_jsonl(runs_dir / f"{run_id}.jsonl")
    ticks: list[tuple] = []
    start = next((e for e in events if e.get("event") == "start"), None)
    diff = next((e for e in events if e.get("event") == "diff"), None)
    t_start = start["ts"] if start else events[0]["ts"]
    env_s = ((start or {}).get("repair_env") or {}).get("seconds") or 0.0
    blast_end = min(t_start - env_s, diff["ts"] if diff else t_start)
    if diff:
        d_end = min(_differ_end(diff, runs_dir), blast_end)
        ticks += [(d_end - (diff.get("latency_s") or 0.0), "mark", ("differ", "running", "")), (d_end, "run", diff)]
        blast_start = d_end
    else:
        blast_start = min(id_time(run_id) or blast_end, blast_end)
    ticks += [(blast_start, "mark", ("blast radius", "running", "")), (blast_end, "worktree", None)]
    if start:
        ticks.append((t_start, "run", start))
    for e in events:
        if e.get("event") in ("diff", "start"):
            continue
        if e.get("event") == "iteration":
            n, T = e["iteration"], e["ts"]
            it0 = T - (e.get("wall_time_s") or 0.0)
            rep_end = it0 + ((e.get("repair") or {}).get("duration_s") or 0.0)
            ticks.append((it0, "muse_start", n))
            muse = runs_dir / f"{run_id}.muse-i{n}.jsonl"
            if muse.exists():
                ticks += spread_muse(n, read_jsonl(muse), it0, rep_end)
            ticks.append((rep_end, "repair_done", n))
            vpath = runs_dir / f"{run_id}.i{n}.verify.json"
            if e.get("verify") and vpath.exists():
                ticks.append((T, "verify", (n, load_verify(vpath))))
        ticks.append((e["ts"], "run", e))
    if start and not any(e.get("event") == "iteration" for e in events):
        # the run died before its first iteration record: show whatever Muse got to
        muse = runs_dir / f"{run_id}.muse-i1.jsonl"
        if muse.exists():
            ticks.append((t_start, "muse_start", 1))
            ticks += spread_muse(1, read_jsonl(muse), t_start, t_start)
            if (runs_dir / f"{run_id}.i1.diff").exists():
                ticks.append((t_start, "mark", ("repair", "ok", "no iteration record: duration not logged")))
                ticks.append((t_start, "repair_done", 1))
    out, last = [], float("-inf")
    for t, kind, payload in ticks:  # logged durations are rounded; never let a later step sort before an earlier one
        last = max(last, t)
        out.append((last, kind, payload))
    return out


class LiveTailer:
    """Polls a run's files and returns new ticks. With run_id None, waits for the first run that starts after
    `since` (what `migrate --follow` uses, since the run id is only chosen once the pipeline starts)."""

    def __init__(self, state_dir: Path, run_id: str | None = None, since: float | None = None,
                 differ: bool = True) -> None:
        self.state_dir = state_dir  # <home>/.driftzero
        self.runs = state_dir / "runs"
        self.run_id = run_id
        self.since = since
        self.differ = differ
        self.offsets: dict[Path, int] = {}
        self.emitted: set = set()
        self.max_iteration_seen = 0
        self.ended = False

    def _once(self, key) -> bool:
        if key in self.emitted:
            return False
        self.emitted.add(key)
        return True

    def _new_lines(self, path: Path) -> list[dict]:
        try:
            with path.open("rb") as fh:
                fh.seek(self.offsets.get(path, 0))
                data = fh.read()
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1  # only whole lines; a partial last line is read next poll
        self.offsets[path] = self.offsets.get(path, 0) + cut
        out = []
        for line in data[:cut].decode("utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _discover(self) -> str | None:
        since = (self.since or 0) - 2
        found = []
        for base, pat in ((self.state_dir, "wt-*"), (self.state_dir / "clones", "*"), (self.runs, "*.jsonl")):
            if not base.is_dir():
                continue
            for p in base.glob(pat):
                rid = p.name.removeprefix("wt-").removesuffix(".jsonl")
                t = id_time(rid)
                if t is not None and t >= since:
                    found.append((t, rid))
        return min(found)[1] if found else None

    def poll(self, now: float | None = None) -> list[tuple]:
        now = time.time() if now is None else now
        ticks: list[tuple] = []
        if self.run_id is None:
            if self.since is not None and self.differ and self._once("differ-start"):
                ticks.append((self.since, "mark", ("differ", "running", "")))
            for spec in self.runs.glob("diff-*.driftspec.json") if self.runs.is_dir() else []:
                if self.since is not None and spec.stat().st_mtime >= self.since - 1 and self._once("spec"):
                    ticks.append((spec.stat().st_mtime, "mark", ("differ", "ok", "")))
                    ticks.append((spec.stat().st_mtime, "mark", ("blast radius", "running", "")))
            rid = self._discover()
            if rid is None:
                return ticks
            self.run_id = rid
            ticks.append((id_time(rid) or now, "discovered", rid))
        rid = self.run_id
        wt = self.state_dir / f"wt-{rid}"
        if wt.exists() and self._once("wt"):
            st = wt.stat()
            ticks.append((getattr(st, "st_birthtime", st.st_mtime), "worktree", None))
        for ev in self._new_lines(self.runs / f"{rid}.jsonl"):
            ticks.append((ev.get("ts", now), "run", ev))
            if ev.get("event") == "end":
                self.ended = True
        n = 1
        while True:
            muse = self.runs / f"{rid}.muse-i{n}.jsonl"
            if not muse.exists():
                break
            prompt = self.runs / f"{rid}.prompt-i{n}.md"
            done = self.runs / f"{rid}.i{n}.diff"
            t_start = prompt.stat().st_mtime if prompt.exists() else now
            if self._once(("muse", n)):
                ticks.append((t_start, "muse_start", n))
            fresh = self._new_lines(muse)
            if done.exists():
                ticks += spread_muse(n, fresh, t_start, done.stat().st_mtime) if fresh else []
                if self._once(("diff", n)):
                    ticks.append((done.stat().st_mtime, "repair_done", n))
            else:
                ticks += [(now, "muse", (n, e)) for e in fresh]
            vpath = self.runs / f"{rid}.i{n}.verify.json"
            if vpath.exists() and ("verify", n) not in self.emitted:
                try:
                    res = load_verify(vpath)
                except ValueError:  # still being written
                    res = None
                if res is not None:
                    self.emitted.add(("verify", n))
                    ticks.append((vpath.stat().st_mtime, "verify", (n, res)))
            n += 1
        order = {"mark": 0, "discovered": 0, "worktree": 2, "muse_start": 3, "muse": 4, "repair_done": 5, "verify": 6}
        indexed = sorted(enumerate(ticks), key=lambda it: (
            it[1][0], (1 if it[1][2].get("event") in ("diff", "start") else 7) if it[1][1] == "run"
            else order.get(it[1][1], 9), it[0]))
        return [tk for _, tk in indexed]


# --------------------------------------------------------------------------- layout

def _fmt_s(x: float | None) -> str:
    if x is None:
        return ""
    return f"{x:5.1f}s" if x < 100 else f"{x:5.0f}s"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _stages_panel(state: WatchState, now: float) -> Panel:
    inner = LEFT_W - 4
    out = Text(no_wrap=True)
    for st in state.stages.values():
        out.append(f"{st.name:<13}", style=FG)
        out.append(f"{st.status:<8}", style=STATUS_STYLE[st.status])
        out.append(f"{_fmt_s(st.elapsed(now)):>{inner - 21}}\n", style=FG if st.status == "running" else DIM)
        out.append(_clip("  " + st.note, inner) + "\n" if st.note else "\n", style=DIM)
    return Panel(out, title=Text("stages", style=ACCENT), title_align="left", box=box.SQUARE,
                 border_style=BORDER, style=f"{FG} on {BG}", width=LEFT_W)


def _muse_panel(state: WatchState, rows: int, width: int) -> Panel:
    n = state.iteration
    view = state.muse.get(n)
    of = f"/{state.max_iterations}" if state.max_iterations and n != state.fix_iteration else ""
    label = "review fix" if n and n == state.fix_iteration else "iteration"
    head = Text()
    head.append(f"{label} {n or '-'}{of}", style="bold " + FG)
    head.append(f"  step {view.steps if view else 0}", style="bold #e3b341")
    head.append(f"  total {state.steps}  tools {state.tool_calls}", style=DIM)
    body = Text(no_wrap=True, overflow="ellipsis")
    inner = width - 4
    for line in list(state.lines)[-max(rows - 4, 1):]:
        style = FG
        if line.startswith("--"):
            style = ACCENT
        elif "model call" in line:
            style = "#d2a8ff"
        elif "[exit 0]" in line or "(success)" in line:
            style = FG
        elif "[exit" in line or line.startswith("    !") or "(failure)" in line:
            style = "#f85149"
        elif line.startswith("muse "):
            style = "#3fb950"
        body.append(_clip(line, inner) + "\n", style=style)
    return Panel(Group(head, Text(""), body), title=Text("Muse Code", style=ACCENT), title_align="left",
                 box=box.SQUARE, border_style=BORDER, style=f"{FG} on {BG}")


def _ok(b: bool | None) -> Text:
    if b is None:
        return Text("--", style=DIM)
    return Text("pass", style=STATUS_STYLE["ok"]) if b else Text("FAIL", style=STATUS_STYLE["failed"])


def _results_panel(state: WatchState, rows: int) -> Panel:
    inner = RIGHT_W - 4
    out: list = []
    g = state.guard
    t = Text()
    t.append("guard  ", style="bold " + FG)
    if g:
        t.append("ok" if g["ok"] else "REJECTED", style=STATUS_STYLE["ok" if g["ok"] else "failed"])
        t.append(f"  i{g['iteration']}", style=DIM)
        t.append(f"\n  {g['rejections']} rejections, {g['review_items']} review items", style=FG)
    else:
        t.append("--", style=DIM)
    out += [t, Text("")]
    v = state.verify
    t = Text()
    t.append("verify ", style="bold " + FG)
    if v:
        t.append(v.backend or "", style=FG)
        t.append(f"  i{v.iteration}", style=DIM)
        if v.wall_s is not None:
            t.append(f"  {v.wall_s:.1f}s", style=DIM)
        t.append("\n  install    ", style=FG)
        t.append("--" if v.install_exit is None else f"exit {v.install_exit}",
                 style=DIM if v.install_exit is None else STATUS_STYLE["ok" if v.install_exit == 0 else "failed"])
        for name, s in (("agent suite", v.agent), ("hold-outs", v.holdout)):
            t.append(f"\n  {name:<12}", style=FG)
            t.append(f"{s.passed}/{s.tests}", style=STATUS_STYLE["ok" if s.exit_code == 0 else "failed"])
        out.append(t)
        budget = max(rows - 18, 2)
        cases = v.holdout.cases
        for name, status in cases[:budget]:
            line = Text("   ")
            line.append_text(_ok(status == "passed"))
            line.append(" " + _clip(name.removeprefix("test_"), inner - 8), style=FG if status == "passed" else
                        STATUS_STYLE["failed"])
            out.append(line)
        if len(cases) > budget:
            out.append(Text(f"   ... {len(cases) - budget} more", style=DIM))
    else:
        t.append("--", style=DIM)
        out.append(t)
    out.append(Text(""))
    r = state.review
    t = Text()
    t.append("review ", style="bold " + FG)
    if r:
        t.append(str(r["verdict"]), style=STATUS_STYLE["ok" if r["verdict"] == "approve" else "running"])
        t.append(f"  {len(r['findings'])} finding(s)" + ("  (2nd)" if r["second"] else ""), style=DIM)
        if state.posted:
            t.append(f"\n  posted as {state.posted} on the PR", style=DIM)
        out.append(t)
        for f in r["findings"][:4]:
            conf = f.get("confidence")
            line = Text("   ")
            line.append(f"{f.get('severity', '?')[:3]:<4}", style=STATUS_STYLE["failed" if f.get("severity") == "high"
                                                                           else "running"])
            line.append(f"{'--' if conf is None else f'{conf:.2f}'} ", style=FG)
            line.append(_clip(f"{f.get('category', '')} {Path(f.get('file', '')).name}", inner - 13), style=DIM)
            out.append(line)
        if len(r["findings"]) > 4:
            out.append(Text(f"   ... {len(r['findings']) - 4} more", style=DIM))
    else:
        t.append("--", style=DIM)
        out.append(t)
    return Panel(Group(*out), title=Text("results", style=ACCENT), title_align="left", box=box.SQUARE,
                 border_style=BORDER, style=f"{FG} on {BG}", width=RIGHT_W)


def _header(state: WatchState) -> Text:
    t = Text(style=f"{FG} on {BG}", no_wrap=True)
    t.append(" DriftZero ", style=f"bold {BG} on {ACCENT}")
    t.append(f"  run {state.run_id or '(waiting for the run to start)'}", style="bold " + FG)
    if state.target:
        t.append(f"   {state.target}", style=FG)
    mode = f"replay {state.replay_speed:g}x" if state.replay_speed else "live"
    if state.done:
        mode += ", finished" + ("" if state.verified is None else ", verified" if state.verified else ", not verified")
    t.append(f"   [{mode}]", style=DIM)
    t.align("left", WIDTH)
    return t


def _footer(state: WatchState, now: float) -> Panel:
    tot = state.totals()
    w = state.wall(now)
    t = Text(no_wrap=True)
    for label, value in (("tokens in", f"{tot['input_tokens']:,}"), ("out", f"{tot['output_tokens']:,}"),
                         ("cached", f"{tot['cached_pct']:.1f}%"), ("USD", f"${tot['usd']:.4f}"),
                         ("wall", f"{int(w // 60)}:{w % 60:04.1f}")):
        t.append(f"{label} ", style=DIM)
        t.append(f"{value}      ", style="bold " + FG)
    return Panel(t, box=box.SQUARE, border_style=BORDER, style=f"{FG} on {BG}")


def render(state: WatchState, now: float, height: int = HEIGHT) -> Layout:
    body_rows = height - 1 - 3
    root = Layout()
    root.split_column(Layout(_header(state), name="header", size=1), Layout(name="body"),
                      Layout(_footer(state, now), name="footer", size=3))
    center_w = WIDTH - LEFT_W - RIGHT_W
    root["body"].split_row(Layout(_stages_panel(state, now), name="left", size=LEFT_W),
                           Layout(_muse_panel(state, body_rows, center_w), name="center"),
                           Layout(_results_panel(state, body_rows), name="right", size=RIGHT_W))
    return root


def make_console(height: int = HEIGHT, record: bool = False, file=None) -> Console:
    return Console(width=WIDTH, height=height, force_terminal=True, color_system="truecolor", record=record,
                   file=file, emoji=False, highlight=False)


def save_frame(console: Console, state: WatchState, now: float, path: Path, height: int = HEIGHT) -> None:
    rec = Console(width=WIDTH, height=height, record=True, file=open(os.devnull, "w"), force_terminal=True,
                  color_system="truecolor", emoji=False, highlight=False)
    rec.print(render(state, now, height))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".svg":
        from rich.terminal_theme import MONOKAI

        path.write_text(rec.export_svg(title=f"driftzero watch {state.run_id or ''}", theme=MONOKAI))
    else:
        path.write_text(rec.export_text())


# --------------------------------------------------------------------------- drivers

def run_replay(runs_dir: Path, run_id: str, speed: float, *, console: Console | None = None, fps: float = 12,
               hold_s: float = 2.0, save: Path | None = None, height: int = HEIGHT,
               max_idle_s: float = 10.0) -> WatchState:
    from rich.live import Live

    ticks = replay_ticks(runs_dir, run_id)
    state = WatchState(run_id=run_id, replay_speed=speed)
    console = console or make_console(height)
    if not ticks:
        return state
    t_first, t_last = ticks[0][0], ticks[-1][0]
    i = 0
    wall0 = time.monotonic()
    with Live(render(state, t_first, height), console=console, refresh_per_second=fps, auto_refresh=False) as live:
        while True:
            vt = t_first + (time.monotonic() - wall0) * speed
            while i < len(ticks) and ticks[i][0] <= vt:
                state.apply(*ticks[i])
                i += 1
            idle = not any(st.status == "running" for st in state.stages.values())
            if i < len(ticks) and idle and ticks[i][0] - vt > max_idle_s:
                # nothing running (e.g. `driftzero review-fix` run later by hand): skip ahead, times stay real
                wall0 -= (ticks[i][0] - vt - 1.0) / speed
                continue
            if i >= len(ticks):
                if not state.done:
                    state.apply(t_last, "exit", "log ended")
                live.update(render(state, t_last, height), refresh=True)
                break
            live.update(render(state, vt, height), refresh=True)
            time.sleep(1.0 / fps)
        time.sleep(hold_s)
    if save:
        save_frame(console, state, t_last, save, height)
    return state


def _alive(pid: int | None) -> bool:
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_live(state_dir: Path, run_id: str | None, *, since: float | None = None, pid: int | None = None,
             differ: bool = True, console: Console | None = None, fps: float = 8, save: Path | None = None,
             height: int = HEIGHT, poll_s: float = 0.25) -> WatchState:
    from rich.live import Live

    tailer = LiveTailer(state_dir, run_id, since=since, differ=differ)
    state = WatchState(run_id=run_id)
    console = console or make_console(height)
    with Live(render(state, time.time(), height), console=console, refresh_per_second=fps) as live:
        while True:
            for tk in tailer.poll():
                state.apply(*tk)
            now = time.time()
            if not state.done and not _alive(pid):
                for tk in tailer.poll():  # the last writes before exit
                    state.apply(*tk)
                if not state.done:
                    state.apply(now, "exit", "process exited")
            live.update(render(state, now, height))
            if state.done:
                break
            time.sleep(poll_s)
    if save:
        save_frame(console, state, time.time(), save, height)
    return state


def spawn_watcher(argv: list[str], cwd: Path, rows: int = HEIGHT + 2) -> str:
    """Start the watcher as its own process in a new terminal (a tmux pane, else a Terminal.app window).
    Returns a description of where it went; raises RuntimeError if no terminal could be opened."""
    import subprocess
    import sys

    cmd = f"cd {shlex.quote(str(cwd))} && " + " ".join(shlex.quote(a) for a in argv)
    if os.environ.get("TMUX"):
        subprocess.Popen(["tmux", "split-window", "-h", "-l", str(WIDTH + 2), cmd])
        return "tmux pane"
    if sys.platform == "darwin":
        quoted = cmd.replace("\\", "\\\\").replace('"', '\\"')
        script = ('tell application "Terminal"\n'
                  f'  do script "{quoted}"\n'
                  f'  set number of columns of front window to {WIDTH + 2}\n'
                  f'  set number of rows of front window to {rows}\n'
                  'end tell')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"osascript failed: {r.stderr.strip()}")
        return "Terminal window"
    raise RuntimeError("no tmux session and not macOS; run the watcher yourself in another terminal")

