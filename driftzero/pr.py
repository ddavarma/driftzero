"""Stage 7: commit the verified repair, push driftzero/<id>, open a GitHub PR with the evidence.

The PR body is built only from recorded artifacts: Muse's schema answer, the DriftSpec,
the blast radius, the guard's findings, the sandbox verification, the cost log and the
run's log files. `load_run` rebuilds the same inputs from `.driftzero/runs/<id>.*` and the
kept worktree, so a body can be regenerated without rerunning Muse:

    python -m driftzero.pr <task_id> [--update <pr-number>]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from driftzero import repair as repair_mod
from driftzero import verify as verify_mod
from driftzero.blast_radius import PROPAGATING, BlastRadius, analyze
from driftzero.differ import DIFF_MODEL
from driftzero.guard import _is_test_path, guard_check
from driftzero.loop import LoopResult
from driftzero.spec import DriftSpec
from driftzero.telemetry import Cost

LOG_CHARS = 6000  # per pytest log inside <details>
REVIEW_TABLE_RULES = ("literal_change", "fixture_data")
FINDING_RULES = ("stale_test_name", "loosened", "modified_test")


@dataclass
class PRResult:
    branch: str
    commit: str
    pushed: bool
    url: str | None
    pr_s: float | None = None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _code(text: object) -> str:
    return f"`{_md(text)}`" if text not in (None, "") else ""


def _s(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s"


def title(res: LoopResult) -> str:
    s = res.spec
    return f"DriftZero: migrate {res.target_slug or res.worktree.project_rel} to {s.api} {s.to_version}"


# --------------------------------------------------------------------------- derived facts

def timing(res: LoopResult, pr_s: float | None = None) -> dict[str, float | None]:
    repair_s = sum(it["repair"]["duration_s"] for it in res.iterations)
    verify_s = sum((it.get("verify") or {}).get("wall_time_s") or 0.0 for it in res.iterations)
    other_s = max(0.0, res.wall_time_s - repair_s - verify_s)
    total = res.wall_time_s + (pr_s or 0.0)
    return {"repair_s": repair_s, "verify_s": verify_s, "other_s": other_s, "pr_s": pr_s, "total_s": total}


def coverage(br: BlastRadius) -> dict[str, set[str]]:
    """file -> tests that reach a non-test symbol in that file through call / ref / fixture / data / member edges."""
    adj: dict[str, set[str]] = defaultdict(set)
    for e in br.edges:
        if e.kind in PROPAGATING:
            adj[e.src].add(e.dst)
    by_id = {s.id: s for s in br.symbols}
    covered: dict[str, set[str]] = defaultdict(set)
    for t in (s for s in br.symbols if s.kind == "test"):
        seen, queue = {t.id}, deque([t.id])
        while queue:
            for nxt in adj[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for sid in seen - {t.id}:
            sym = by_id.get(sid)
            if sym is not None and sym.kind != "test":
                covered[sym.file].add(t.id)
    return covered


def logs_location(root: Path, task_id: str) -> tuple[str, str, list[str]] | None:
    """(https base, branch, files) for docs/runs/<id>.* committed on a pushed branch, else None."""
    sha = _git(root, "log", "--all", "-1", "--format=%H", "--", f"docs/runs/{task_id}.jsonl")
    if not sha:
        return None
    remotes = _git(root, "branch", "-r", "--contains", sha, "--format=%(refname:short)").split()
    remotes = [r for r in remotes if r.startswith("origin/") and r != "origin/HEAD"]
    if not remotes:
        return None
    ref = "origin/main" if "origin/main" in remotes else remotes[0]
    files = [f for f in _git(root, "ls-tree", "--name-only", ref, "docs/runs/").split()
             if Path(f).name.startswith(task_id + ".")]
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", _git(root, "remote", "get-url", "origin"))
    if not m or not files:
        return None
    branch = ref.removeprefix("origin/")
    return f"https://github.com/{m.group(1)}/blob/{branch}", branch, files


# --------------------------------------------------------------------------- sections

def _lead(res: LoopResult, diff: repair_mod.RepairDiff, t: dict) -> list[str]:
    lib = sum(1 for p, _, _ in diff.files if not _is_test_path(p))
    tests = len(diff.files) - lib
    v, g, c = res.verify, res.guard, res.cost
    verified = (f"agent suite {v.agent.passed_count}/{v.agent.tests} · hold-out {v.holdout.passed_count}/{v.holdout.tests} "
                f"(never shown to the agent) · guard {'ok' if g and g.ok else 'REJECTED'}, "
                f"{len(g.rejections) if g else 0} rejections · "
                f"{'Modal sandbox' if v.backend == 'modal' else 'local'} · "
                f"{len(res.iterations)} iteration{'s' if len(res.iterations) != 1 else ''}") if v else "not verified"
    usd = "n/a" if c.usd is None else f"${c.usd:.4f}"
    pr_part = f" + {_s(t['pr_s'])} PR" if t["pr_s"] is not None else ""
    return [
        f"> **Changed** {len(diff.files)} files (+{diff.added} / −{diff.deleted}) in "
        f"`{res.target_slug or res.worktree.project_rel}`: "
        f"{lib} library, {tests} test / fixture<br>",
        f"> **Verified** {verified}<br>",
        f"> **Cost** {usd} · {_s(res.wall_time_s)} repair loop{pr_part} · `{res.model}`, {res.steps} model steps",
    ]


def _findings(res: LoopResult) -> list[str]:
    from driftzero.reviewer import REVIEWER_LABEL

    g = res.guard
    labels = {"stale_test_name": "stale test name", "loosened": "comparison loosened", "modified_test": "test edited"}
    rows = []
    for f in (g.review_items if g else []):
        if f.rule not in FINDING_RULES:
            continue
        where = f"{f.file}::{f.test}" if f.test else f.file
        detail = f.message if f.rule != "loosened" else f"{f.message}: {_code(f.old)} → {_code(f.new)}"
        rows.append(f"| guard (static) | – | {labels[f.rule]} | {_code(where)} | {_md(detail)} |")
    for it in res.iterations:
        if not it["guard"]["ok"]:
            reasons = "; ".join(r["message"] for r in it["guard"]["rejections"])
            rows.append(f"| guard (static) | rejected | iteration {it['iteration']} | – | {_md(reasons)} |")
    rv = res.review
    if rv is not None and rv.result is not None:
        for f in rv.result.findings:
            where = f.file + (f":{f.line_hint}" if f.line_hint else "")
            rows.append(f"| {REVIEWER_LABEL} | {f.severity} | {f.category} | {_code(where)} | "
                        f"{_md(f.explanation)} *Fix:* {_md(f.suggested_fix)} |")
    out = ["| source | severity | kind | where | finding |", "|---|---|---|---|---|", *rows] if rows else []
    if rv is not None:
        if rv.status == "incomplete":
            status = ("**reviewer_incomplete**: ran out of its token budget before finishing; this is not an "
                      "approval" + (f" (partial findings: {len(rv.result.findings)})" if rv.result else ""))
        elif rv.result:
            status = f"verdict **{rv.result.verdict}**, {len(rv.result.findings)} finding(s)"
        else:
            status = f"review failed ({_md(rv.error)})"
        out += ["", f"{REVIEWER_LABEL}: `{rv.model}` on {rv.provider.title()}, reasoning_effort={rv.effort}: {status}; "
                f"${rv.cost.usd or 0:.4f}, {rv.latency_s:.0f} s. It is told what the guard checks and reviews what "
                "the guard cannot see (test-shaped library code, precision, unrequested behaviour changes, "
                "compatibility paths)."]
    if not out:
        return ["No findings: the guard saw no stale names, loosened comparisons or restructured tests, and no "
                "model review ran."]
    return ["None of these block the merge; each needs a human decision.", "", *out]


def _spec_table(res: LoopResult) -> list[str]:
    s = res.spec
    rows = ["| change | where | detail |", "|---|---|---|"]
    rows += [f"| endpoint renamed | {_code(e.method + ' ' + e.old)} | → {_code(e.new)} |" for e in s.endpoints_renamed]
    rows += [f"| field removed | {_code(f.schema_name + '.' + f.path)} | {_md(f.old_type)}"
             f"{'; replaced by ' + _code(f.replacement) if f.replacement else ''} |" for f in s.fields_removed]
    rows += [f"| field renamed | {_code(f.schema_name + '.' + f.old_path)} | → {_code(f.new_path)} |" for f in s.fields_renamed]
    rows += [f"| type changed | {_code(f.schema_name + '.' + f.path)} | {_md(f.old_type)} → {_md(f.new_type)} |"
             for f in s.type_changes]
    rows += [f"| value changed | {_code(f.schema_name + '.' + f.path)} | {_code(f.old)} → {_code(f.new)} |"
             for f in s.value_changes]
    rows += [f"| field added | {_code(f.schema_name + '.' + f.path)} | {_md(f.type)}{' (required)' if f.required else ''} |"
             for f in s.fields_added]
    return rows


def _blast_table(res: LoopResult) -> list[str]:
    br = res.blast_radius
    by_file: dict[str, list] = defaultdict(list)
    for sym in br.symbols:
        by_file[sym.file].append(sym)
    cov = coverage(br)
    rows = ["| file | impacted functions / classes | covered by tests |", "|---|---|---|"]
    for file in br.files:
        syms = [s for s in by_file[file] if s.kind != "test"]
        in_file = sum(1 for s in by_file[file] if s.kind == "test")
        names = ", ".join(f"**{s.name}**" if s.direct else s.name for s in syms) or "–"
        if cov.get(file):
            covered = f"{len(cov[file])}"
        elif in_file:
            covered = f"{in_file} (tests in this file)"
        else:
            covered = "**none**"
        rows.append(f"| {_code(file)} | {names} | {covered} |")
    n_direct = sum(s.direct for s in br.symbols)
    rows += ["", f"{len(br.files)} files, {len(br.symbols)} symbols ({n_direct} direct, in **bold**; the rest depend on "
             f"them), {len(br.tests)} tests. *Covered by tests* counts the tests that reach a symbol in the file through "
             f"calls, references or fixtures in the analyzer's link graph. Deterministic `ast` analysis, no LLM. "
             f"Test data carrying old fields: {', '.join(_code(d.file) for d in br.data_files) or 'none'}."]
    return rows


def _test_changes(res: LoopResult) -> list[str]:
    g = res.guard
    if g is None:
        return ["_Guard did not run._"]
    table = [f for f in g.review_items if f.rule in REVIEW_TABLE_RULES]
    added = [f for f in g.review_items if f.rule == "added_test"]
    out = ["The guard rejects removed tests, fewer asserts, skips/xfails, new conftest.py and pytest-config changes; "
           "none were present." if g.ok else "**The guard rejected this diff.**", ""]
    if table:
        out += ["| kind | where | change |", "|---|---|---|"]
        for f in table:
            where = f"{f.file}::{f.test}" if f.test else f.file
            if f.rule == "literal_change":
                out.append(f"| literal changed | {_code(where)} | {_code(f.old)} → {_code(f.new)} |")
            else:
                out.append(f"| test data | {_code(where)} | {_md(f.message)} |")
        out.append("")
    if added:
        files = sorted({f.file for f in added})
        asserts = sum(int(f.new or 0) for f in added)
        out.append(f"**Added:** {len(added)} new test{'s' if len(added) != 1 else ''} in "
                   f"{', '.join(_code(f) for f in files)} ({asserts} asserts total).")
    if not table and not added:
        out.append("No test changes.")
    return out


def _verify_section(res: LoopResult) -> list[str]:
    v = res.verify
    if v is None:
        return ["_Not verified._"]
    a, h = v.agent, v.holdout
    out = [
        "| | result |", "|---|---|",
        f"| agent's test suite (`tests/`) | **{a.passed_count}/{a.tests}** passed, exit {a.exit_code} |",
        f"| hold-out contract tests (never shown to the agent) | **{h.passed_count}/{h.tests}** passed, exit {h.exit_code} |",
        f"| sandbox | {'Modal ' + _code(v.sandbox_id) if v.backend == 'modal' else 'local (--local)'}, "
        f"debian_slim py3.11, `pip install -e` exit {v.setup.get('exit_code')} |",
        f"| sandbox wall time | {v.wall_time_s:.1f} s |",
        f"| repair iterations | {len(res.iterations)} |",
    ]
    cases = h.cases()
    if cases:
        icon = {"passed": "✅", "failed": "❌", "error": "❌", "skipped": "⏭️"}
        out += ["", "Hold-out tests (derived only from `openapi_v2.json` and the changelog; run with "
                "`--noconftest -c /dev/null`):", ""]
        out += [f"- {icon.get(st, '?')} `{name}` {st}" for name, st in cases]
    return out


def _cost_section(res: LoopResult) -> list[str]:
    c = res.cost
    usd = "n/a" if c.usd is None else f"${c.usd:.4f}"
    return [
        "| | |", "|---|---|",
        f"| model | `{res.model}` via Muse Code (`muse exec`) |",
        f"| model steps | {res.steps} |",
        f"| tokens | {c.input_tokens:,} input ({c.cached_pct:.1f}% cached), {c.output_tokens:,} output "
        f"({c.reasoning_tokens:,} reasoning) |",
        f"| cost | **{usd}** (Meta rates as of 2026-09) |",
    ] + ([f"| of which DriftSpec generation | ${res.diff_cost.usd or 0:.4f}: one `{DIFF_MODEL}` call, "
          f"{res.diff_cost.input_tokens:,} in / {res.diff_cost.output_tokens:,} out |"] if res.diff_cost else []) + ([f"| code review (open weights) | ${res.review.cost.usd or 0:.4f}: `{res.review.model}` on "
          f"{res.review.provider.title()}, {res.review.cost.input_tokens:,} in / {res.review.cost.output_tokens:,} out "
          f"({res.review.cost.reasoning_tokens:,} reasoning) |"] if res.review is not None else [])


def _log_block(label: str, suite) -> list[str]:
    text = (suite.stdout + ("\n" + suite.stderr if suite.stderr.strip() else "")).strip()
    if len(text) > LOG_CHARS:
        text = text[:LOG_CHARS] + "\n... (truncated)"
    return ["<details>", f"<summary>{label}: <code>{' '.join(suite.command)}</code> (exit {suite.exit_code})</summary>",
            "", "```", text, "```", "</details>"]


_LOG_LABELS = [
    (r"\.jsonl$", "loop log: one record per iteration, tokens, USD", r"\.muse-i\d+\.jsonl$"),
    (r"\.muse-i\d+\.jsonl$", "Muse Code JSONL events", None),
    (r"\.prompt-i\d+\.md$", "repair prompt sent to Muse", None),
    (r"\.i\d+\.diff$", "repair diff", None),
    (r"\.verify\.json$", "verification result", None),
    (r"\.(agent|holdout)\.(xml|log)$", "pytest JUnit / log", None),
    (r"\.pr\.md$", "this PR body as first generated", None),
    (r"\.stderr\.log$", "Muse Code stderr", None),
    (r"\.driftspec\.generated\.json$", "DriftSpec generated by the differ from the changelog + OpenAPI", None),
]


def _log_label(name: str) -> str:
    for pattern, label, exclude in _LOG_LABELS:
        if re.search(pattern, name) and not (exclude and re.search(exclude, name)):
            return label
    return ""


def _logs_section(res: LoopResult, t: dict, logs: tuple[str, str, list[str]] | None) -> list[str]:
    out = [f"**Wall time:** repair {_s(t['repair_s'])} · verify {_s(t['verify_s'])} · "
           f"blast radius, guard and setup {_s(t['other_s'])} · PR {_s(t['pr_s'])} · total {_s(t['total_s'])}", ""]
    if logs:
        base, branch, files = logs
        out.append(f"Run files on `{branch}`:")
        out.append("")
        for f in files:
            label = _log_label(Path(f).name)
            out.append(f"- [`{Path(f).name}`]({base}/{f}){' · ' + label if label else ''}")
    else:
        out.append(f"Run files are local at `.driftzero/runs/{res.task_id}.*` (not committed).")
    if res.verify:
        out += ["", *_log_block("agent suite", res.verify.agent), "", *_log_block("hold-out suite", res.verify.holdout)]
    return out


def reproduce_command(res: LoopResult, spec_path: str | None) -> str:
    if res.command:
        prefix = "" if res.target_url else f"git checkout {res.worktree.base[:7]} && "
        return prefix + res.command
    repo_rel = res.worktree.project_rel
    spec_rel = os.path.relpath(spec_path, res.worktree.root) if spec_path else f"{repo_rel}/driftspec.json"
    return f"git checkout {res.worktree.base[:7]} && driftzero migrate --repo {repo_rel} --spec {spec_rel}"


def render_body(res: LoopResult, *, pr_s: float | None = None, spec_path: str | None = None,
                logs: tuple[str, str, list[str]] | None = None, diff: repair_mod.RepairDiff | None = None) -> str:
    s = res.spec
    diff = diff or repair_mod.collect_diff(res.worktree)
    t = timing(res, pr_s)
    summary = res.answer.summary if res.answer else "_Muse did not return a structured summary._"
    lines = [
        *_lead(res, diff, t), "",
        f"Automated migration of `{res.target_slug or res.worktree.project_rel}` from "
        f"**{s.api} {s.from_version} → {s.to_version}**, "
        f"repaired by Muse Code (`{res.model}`) in an isolated git worktree and verified in a sandbox.",
        "",
        "## Agent's own summary", "", summary, "",
        "## Review findings", "", *_findings(res), "",
        "## API change (DriftSpec)", "", *_spec_table(res), "",
        "## Blast radius", "", *_blast_table(res), "",
        "## Test changes, review required", "", *_test_changes(res), "",
        "## Verification", "", *_verify_section(res), "",
        "## Cost", "", *_cost_section(res), "",
        "## Logs", "", *_logs_section(res, t, logs), "",
        "## Reproduce", "", f"`{reproduce_command(res, spec_path)}`", "",
        f"<sub>DriftZero task `{res.task_id}` · branch `{res.worktree.branch}` · base `{res.worktree.base[:10]}`</sub>",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- rebuild a finished run

def load_run(root: Path, task_id: str) -> tuple[LoopResult, str | None]:
    """Rebuild a LoopResult from .driftzero/runs/<id>.* and its kept worktree (no model calls).

    The guard is re-run on the worktree (it is deterministic, and picks up rules added since).
    Returns (result, spec_path).
    """
    runs = root / ".driftzero" / "runs"
    events = [json.loads(line) for line in (runs / f"{task_id}.jsonl").read_text().splitlines()]
    start = next(e for e in events if e["event"] == "start")
    end = next(e for e in events if e["event"] == "end")
    its = [e for e in events if e["event"] == "iteration"]
    repo = Path(start["source_root"]) if start.get("target_url") else Path(start["repo"])
    spec_path = start.get("spec_path")
    if not spec_path:
        found = sorted(repo.glob("driftspec*.json"))
        spec_path = str(found[0]) if len(found) == 1 else None
    spec = DriftSpec.load(spec_path)
    source_root = Path(start.get("source_root") or root)
    wt = repair_mod.Worktree(root=source_root, path=Path(start["worktree"]), branch=start["branch"], base=start["base"],
                             project_rel=os.path.relpath(repo if not start.get("target_url") else source_root,
                                                         source_root), home=root)
    last = its[-1]["iteration"]
    vpath = runs / f"{task_id}.i{last}.verify.json"
    vres = verify_mod.VerifyResult.model_validate_json(vpath.read_text()) if vpath.exists() else None
    trace = repair_mod.MuseTrace()
    for line in (runs / f"{task_id}.muse-i{last}.jsonl").read_text().splitlines():
        trace.feed_line(line)
    answer = repair_mod.RepairAnswer.model_validate_json(trace.final_text) if trace.final_text else None
    cost = Cost()
    for it in its:
        c = it["cost"]
        cost = cost + Cost(c["input_tokens"], c["cached_tokens"], c["output_tokens"], c.get("reasoning_tokens", 0),
                           c.get("usd"))
    guard = guard_check(wt, repair_mod.collect_diff(wt))
    diff_ev = next((e for e in events if e["event"] == "diff"), None)
    diff_cost = None
    if diff_ev and diff_ev.get("cost"):
        d = diff_ev["cost"]
        diff_cost = Cost(d["input_tokens"], d["cached_tokens"], d["output_tokens"], d.get("reasoning_tokens", 0), d.get("usd"))
        cost = cost + diff_cost
    res = LoopResult(task_id=task_id, worktree=wt, spec=spec, blast_radius=analyze(repo, spec),
                     passed=end["passed"], iterations=its, log_path=runs / f"{task_id}.jsonl", cost=cost,
                     wall_time_s=end["wall_time_s"], model=start.get("model") or repair_mod.DEFAULT_MODEL,
                     steps=sum(it["repair"]["steps"] for it in its), answer=answer, guard=guard, verify=vres,
                     ended_at=end["ts"], target_url=start.get("target_url"), target_slug=start.get("target_slug"),
                     diff_cost=diff_cost, command=start.get("command"))
    from driftzero.reviewer import latest_logged

    res.review = latest_logged([runs / f"{task_id}.jsonl", runs / f"{task_id}.review.jsonl"])
    if res.review is not None and res.review.cost.usd is not None:
        res.cost = res.cost + res.review.cost
    return res, spec_path


# --------------------------------------------------------------------------- git / gh

def commit_and_push(res: LoopResult) -> tuple[str, bool]:
    wt = res.worktree
    _git(wt.path, "add", "-A")  # hidden hold-outs are skip-worktree, so they stay out of the commit
    msg = f"{title(res)}\n\n" + (res.answer.summary if res.answer else "") + \
          f"\n\nRepaired by Muse Code ({res.model}); verified: " + \
          (res.verify.summary() if res.verify else "n/a") + f"\nDriftZero-Task: {res.task_id}\n"
    _git(wt.path, "commit", "-q", "-m", msg)
    sha = _git(wt.path, "rev-parse", "HEAD")
    _git(wt.path, "push", "-q", "-u", "origin", wt.branch)
    return sha, True


def open_pr(res: LoopResult, *, create: bool = True, base: str = "main", spec_path: str | None = None) -> PRResult:
    """Commit, push the branch, and (unless create=False) open the PR. Only call on a verified result.

    The body is written twice: once to open the PR, then again with the measured PR-step time.
    """
    if not res.passed:
        raise ValueError("refusing to open a PR for an unverified repair")
    t0 = time.monotonic()
    diff = repair_mod.collect_diff(res.worktree)
    sha, pushed = commit_and_push(res)
    body_path = res.log_path.with_suffix(".pr.md")
    body_path.write_text(render_body(res, spec_path=spec_path, diff=diff))
    url = None
    if create:
        repo_flag = ["-R", res.target_slug] if res.target_slug else []
        url = subprocess.run(["gh", "pr", "create", *repo_flag, "--base", base, "--head", res.worktree.branch,
                              "--title", title(res), "--body-file", str(body_path)],
                             cwd=res.worktree.path, check=True, capture_output=True, text=True).stdout.strip()
    pr_s = round(time.monotonic() - t0, 1)
    if url:
        body_path.write_text(render_body(res, pr_s=pr_s, spec_path=spec_path, diff=diff))
        subprocess.run(["gh", "pr", "edit", url, "--body-file", str(body_path)], cwd=res.worktree.path,
                       check=True, capture_output=True, text=True)
    with res.log_path.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "event": "pr", "task_id": res.task_id, "branch": res.worktree.branch,
                             "commit": sha, "url": url, "pr_s": pr_s}) + "\n")
    return PRResult(branch=res.worktree.branch, commit=sha, pushed=pushed, url=url, pr_s=pr_s)


def review_pr(res: LoopResult, pr_url: str, *, provider: str = "baseten", effort: str | None = None,
              strict: bool = False, changelog: str | None = None, outcome=None, log_path: Path | None = None) -> dict:
    """Run the open-weights reviewer on the committed repair (or use `outcome`), post it as a PR review."""
    from driftzero import reviewer

    wt = res.worktree
    diff = _git(wt.path, "diff", wt.base, "HEAD")
    if outcome is None:
        if changelog is None:
            logs = sorted(wt.project.glob("CHANGELOG*.md"))
            changelog = logs[0].read_text() if logs else None
        outcome = reviewer.review(diff, [str(f) for f in (res.guard.review_items if res.guard else [])], res.spec,
                                  changelog, provider=provider, effort=effort or reviewer.DEFAULT_EFFORT,
                                  log_path=log_path or res.log_path, task_id=res.task_id,
                                  test_context=reviewer.test_context_for(res, diff))
    res.review = outcome
    slug = res.target_slug or reviewer.repo_slug(wt.root)
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    posted = reviewer.post_review(slug, number, outcome, diff, strict=strict, cwd=wt.path)
    with res.log_path.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "event": "review_posted", "task_id": res.task_id, **posted}) + "\n")
    return posted


def update_body(res: LoopResult, url: str, *, pr_s: float | None = None, spec_path: str | None = None) -> Path:
    body_path = res.log_path.with_suffix(".pr.md")
    body_path.write_text(render_body(res, pr_s=pr_s, spec_path=spec_path))
    subprocess.run(["gh", "pr", "edit", url, "--body-file", str(body_path)], cwd=res.worktree.path, check=True,
                   capture_output=True, text=True)
    return body_path


def regenerate(root: Path, task_id: str, update: str | None = None) -> tuple[str, Path]:
    """Re-render a finished run's PR body from saved data; optionally `gh pr edit` it."""
    res, spec_path = load_run(root, task_id)
    events = [json.loads(line) for line in res.log_path.read_text().splitlines()]
    pr_s = next((e["pr_s"] for e in events if e["event"] == "pr"), None)
    if pr_s is None and update and res.ended_at:
        # runs from before the `pr` event existed: PR creation time on GitHub minus loop end
        repo_flag = ["-R", res.target_slug] if res.target_slug else []
        created = subprocess.run(["gh", "pr", "view", update, *repo_flag, "--json", "createdAt", "-q", ".createdAt"], cwd=root,
                                 check=True, capture_output=True, text=True).stdout.strip()
        pr_s = round(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() - res.ended_at, 1)
    body = render_body(res, pr_s=pr_s, spec_path=spec_path, logs=logs_location(root, task_id))
    out = res.log_path.with_suffix(".pr-v2.md")
    out.write_text(body)
    if update:
        subprocess.run(["gh", "pr", "edit", update, *(["-R", res.target_slug] if res.target_slug else []),
                        "--body-file", str(out)], cwd=root, check=True,
                       capture_output=True, text=True)
    return body, out


if __name__ == "__main__":
    tid = sys.argv[1]
    upd = sys.argv[sys.argv.index("--update") + 1] if "--update" in sys.argv else None
    body_text, path = regenerate(Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")), tid, upd)
    print(body_text)
