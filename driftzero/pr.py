"""Stage 7: commit the verified repair, push driftzero/<id>, open a GitHub PR with the evidence.

The PR body is built only from recorded artifacts: Muse's schema answer, the DriftSpec,
the blast radius, the guard's review items, the sandbox verification and the cost log.
"""
from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from driftzero.loop import LoopResult

LOG_CHARS = 6000  # per pytest log inside <details>


@dataclass
class PRResult:
    branch: str
    commit: str
    pushed: bool
    url: str | None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _code(text: object) -> str:
    return f"`{_md(text)}`" if text not in (None, "") else ""


def title(res: LoopResult) -> str:
    s = res.spec
    return f"DriftZero: migrate {res.worktree.project_rel} to {s.api} {s.to_version}"


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
    rows = ["| file | impacted functions / classes | tests |", "|---|---|---|"]
    for file in br.files:
        syms = [s for s in by_file[file] if s.kind != "test"]
        tests = [s for s in by_file[file] if s.kind == "test"]
        names = ", ".join(f"**{s.name}**" if s.direct else s.name for s in syms) or "–"
        rows.append(f"| {_code(file)} | {names} | {len(tests) or '–'} |")
    n_direct = sum(s.direct for s in br.symbols)
    rows.append("")
    rows.append(f"{len(br.files)} files, {len(br.symbols)} symbols ({n_direct} direct, in **bold**; the rest depend on "
                f"them), {len(br.tests)} tests. Deterministic `ast` analysis, no LLM. "
                f"Test data carrying old fields: {', '.join(_code(d.file) for d in br.data_files) or 'none'}.")
    return rows


def _guard_section(res: LoopResult) -> list[str]:
    g = res.guard
    if g is None:
        return ["_Guard did not run._"]
    if not g.review_items:
        return ["The guard found no test changes."]
    labels = {"literal_change": "literal changed", "fixture_data": "test data", "loosened": "comparison loosened",
              "added_test": "test added", "modified_test": "test edited"}
    out = ["The guard rejects removed tests, fewer asserts, skips/xfails, new conftest.py and pytest-config changes "
           "(none present). These edits are allowed but need a human look:", "",
           "| kind | where | change |", "|---|---|---|"]
    for f in g.review_items:
        where = f"{f.file}::{f.test}" if f.test else f.file
        change = f"{_code(f.old)} → {_code(f.new)}" if f.rule == "literal_change" else _md(f.message)
        out.append(f"| {labels.get(f.rule, f.rule)} | {_code(where)} | {change} |")
    rejected = [it for it in res.iterations if not it["guard"]["ok"]]
    if rejected:
        out += ["", f"Earlier iterations rejected by the guard: {', '.join(str(it['iteration']) for it in rejected)}."]
    return out


def _verify_section(res: LoopResult) -> list[str]:
    v = res.verify
    if v is None:
        return ["_Not verified._"]
    a, h = v.agent, v.holdout
    return [
        "| | result |", "|---|---|",
        f"| agent's test suite (`tests/`) | **{a.passed_count}/{a.tests}** passed, exit {a.exit_code} |",
        f"| hold-out contract tests (never shown to the agent) | **{h.passed_count}/{h.tests}** passed, exit {h.exit_code} |",
        f"| sandbox | {'Modal ' + _code(v.sandbox_id) if v.backend == 'modal' else 'local (--local)'}, "
        f"debian_slim py3.11, `pip install -e` exit {v.setup.get('exit_code')} |",
        f"| sandbox wall time | {v.wall_time_s:.1f} s |",
        f"| repair iterations | {len(res.iterations)} |",
    ]


def _cost_section(res: LoopResult) -> list[str]:
    c = res.cost
    usd = "n/a" if c.usd is None else f"${c.usd:.4f}"
    return [
        "| | |", "|---|---|",
        f"| model | `{res.model}` via Muse Code (`muse exec`) |",
        f"| model steps | {res.steps} |",
        f"| tokens | {c.input_tokens:,} input ({c.cached_pct:.1f}% cached), {c.output_tokens:,} output "
        f"({c.reasoning_tokens:,} reasoning) |",
        f"| cost | **{usd}** (rates as of 2026-09) |",
        f"| end-to-end wall time | {res.wall_time_s:.0f} s |",
    ]


def _log_block(label: str, suite) -> list[str]:
    text = (suite.stdout + ("\n" + suite.stderr if suite.stderr.strip() else "")).strip()
    if len(text) > LOG_CHARS:
        text = text[:LOG_CHARS] + "\n... (truncated)"
    return ["<details>", f"<summary>{label}: <code>{' '.join(suite.command)}</code> (exit {suite.exit_code})</summary>",
            "", "```", text, "```", "</details>"]


def render_body(res: LoopResult) -> str:
    s = res.spec
    summary = res.answer.summary if res.answer else "_Muse did not return a structured summary._"
    lines = [
        f"Automated migration of `{res.worktree.project_rel}` from **{s.api} {s.from_version} → {s.to_version}**, "
        f"repaired by Muse Code (`{res.model}`) in an isolated worktree and verified in a sandbox.",
        "",
        "## Summary", "", summary, "",
        "## API change (DriftSpec)", "", *_spec_table(res), "",
        "## Blast radius", "", *_blast_table(res), "",
        "## Test changes, review required", "", *_guard_section(res), "",
        "## Verification", "", *_verify_section(res), "",
        "## Cost", "", *_cost_section(res), "",
    ]
    if res.verify:
        lines += ["## Logs", "", *_log_block("agent suite", res.verify.agent), "",
                  *_log_block("hold-out suite", res.verify.holdout), ""]
    lines += [f"<sub>DriftZero task `{res.task_id}` · branch `{res.worktree.branch}` · base `{res.worktree.base[:10]}`</sub>"]
    return "\n".join(lines) + "\n"


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


def open_pr(res: LoopResult, *, create: bool = True, base: str = "main") -> PRResult:
    """Commit, push the branch, and (unless create=False) open the PR. Only call on a verified result."""
    if not res.passed:
        raise ValueError("refusing to open a PR for an unverified repair")
    sha, pushed = commit_and_push(res)
    body_path = res.log_path.with_suffix(".pr.md")
    body_path.write_text(render_body(res))
    url = None
    if create:
        url = subprocess.run(["gh", "pr", "create", "--base", base, "--head", res.worktree.branch,
                              "--title", title(res), "--body-file", str(body_path)],
                             cwd=res.worktree.path, check=True, capture_output=True, text=True).stdout.strip()
    return PRResult(branch=res.worktree.branch, commit=sha, pushed=pushed, url=url)
