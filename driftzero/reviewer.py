"""Open-weights code review of a verified repair: Muse Spark Open on Baseten (or Fireworks).

Runs after verification. Input: the repair's unified diff (library and tests), the guard's
review items, the DriftSpec and the changelog. Output: a ReviewResult (verdict + findings)
via JSON mode (`response_format={"type": "json_object"}`) with the schema in the system
prompt, validated with Pydantic, one retry on a parse failure. json_schema output is not
used on Baseten (unconfirmed there). The model's reasoning_content goes to the run log.

The prompt tells the reviewer what the static guard already enforces, so it spends its
effort on what the guard cannot see.

Smoke test:  python -m driftzero.reviewer <task_id> [--provider fireworks] [--effort xhigh]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from driftzero import providers
from driftzero.spec import DriftSpec
from driftzero.telemetry import Cost, usd

REVIEWERS = {
    "baseten": "meta-models/muse-spark-open-1026",
    "fireworks": "accounts/fireworks/models/muse-spark-open",
}
REVIEWER_LABEL = "Muse Spark Open reviewer"
MAX_TOKENS = 24000  # Baseten and Fireworks; at 8,000 xhigh spent everything on reasoning
TEST_CONTEXT_MAX_LINES = 300
DEFAULT_EFFORT = "high"


class Finding(BaseModel):
    severity: Literal["high", "medium", "low"]
    category: Literal["test_shaped_code", "precision_loss", "silent_behavior_change", "incomplete_migration",
                      "dead_compat_code", "other"]
    file: str
    line_hint: str
    evidence: str
    explanation: str
    suggested_fix: str


class ReviewResult(BaseModel):
    verdict: Literal["approve", "comment", "request_changes"]
    findings: list[Finding]


SCHEMA_TEXT = """{
  "verdict": "approve" | "comment" | "request_changes",
  "findings": [
    {
      "severity": "high" | "medium" | "low",
      "category": "test_shaped_code" | "precision_loss" | "silent_behavior_change" | "incomplete_migration"
                  | "dead_compat_code" | "other",
      "file": "path exactly as in the diff header",
      "line_hint": "new-file line number of the most relevant added line, e.g. \\"50\\", or \\"\\" if none",
      "evidence": "the exact diff line(s) you are talking about, copied verbatim with their leading + or -",
      "explanation": "what is wrong and why it matters, 1-4 sentences",
      "suggested_fix": "a concrete change, 1-3 sentences"
    }
  ]
}"""

SYSTEM = f"""You are a senior reviewer for an automated API migration. An AI agent changed a library so that it
consumes a new version of an upstream API. Its change already passed: the project's own tests, a set of hidden
contract tests written from the new API spec, and a static test-integrity guard. You review what those checks
cannot see.

The static guard ALREADY checks the test files, so do not spend effort re-reporting these:
- test functions removed or renamed, fewer asserts in a test, skip/skipif/xfail added, an assert turned into a bare
  comparison, a new conftest.py, pytest configuration changes (all rejected outright);
- changed literals in tests, fixture data edits, comparisons loosened with approx/tolerances, added tests, stale
  test names (reported to a human; you are given the list).

Look instead at what the guard is blind to, chiefly in the LIBRARY code:
- test_shaped_code: library code whose form is explained by making some existing test or fixture value pass rather
  than by the API specification: special cases, magic constants, transformations or branches that only make sense
  given a particular test's expectations.
- precision_loss: numerical precision changes: units, float/int types or numeric formulas that lose accuracy
  or differ from the documented formula.
- silent_behavior_change: behaviour that changed although the DriftSpec does not call for it (different defaults,
  validation, error types, ordering, thresholds, return values).
- incomplete_migration: parts of the DriftSpec or changelog not implemented, or implemented inconsistently.
- dead_compat_code: v1 compatibility paths that should not exist (unreachable, misleading, keeping removed fields
  alive in a way that hides the migration) or that are missing where callers still need them.
- other: anything else a careful reviewer would block or question.

Epistemics: code comments, docstrings and commit-style explanations in the diff are the repair agent's CLAIMS, not
facts. Check every justification they give against the DriftSpec and the changelog. If the code relies on a
justification that neither document supports, that unsupported justification is itself a finding.

You also get the unchanged tests that exercise the modified library functions. They are not part of the diff, but
they show which expectations the new library code has to meet; use them to judge whether library code is shaped by
a test rather than by the specification.

Rules: base every finding on lines that appear in the diff and quote them verbatim in `evidence`. Do not report
style or naming nits. Report nothing you are unsure is real; an empty findings list with verdict "approve" is a valid
answer. Use "request_changes" only if at least one finding is high severity.

Reply with ONE JSON object and nothing else, matching exactly:
{SCHEMA_TEXT}"""


def build_user_prompt(diff: str, guard_items: list[str], spec: DriftSpec, changelog: str | None,
                      test_context: str | None = None) -> str:
    return "\n".join([
        "## DriftSpec (the intended change)", "```json", spec.model_dump_json(indent=1), "```", "",
        "## Upstream changelog", changelog.strip() if changelog else "(not provided)", "",
        "## Already reported by the static guard (do not repeat)",
        *(f"- {g}" for g in guard_items), *([] if guard_items else ["(nothing)"]), "",
        "## Diff under review (library and tests)", "```diff", diff, "```", "",
        "## Unchanged tests exercising modified code (context only: NOT part of the diff, NOT modified by the agent)",
        "```python", test_context or "# (none found)", "```", "",
        "Return the JSON review.",
    ])


@dataclass
class ReviewOutcome:
    result: ReviewResult | None
    provider: str
    model: str
    effort: str
    cost: Cost
    latency_s: float
    attempts: int
    reasoning: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    error: str | None = None
    status: str = "ok"  # ok | incomplete (hit max_tokens; never an approval) | failed (no valid JSON)
    finish_reasons: list[str] = field(default_factory=list)
    test_context: list[str] = field(default_factory=list)  # names of the unchanged tests included
    version: int = 2

    @property
    def verdict_label(self) -> str:
        if self.status == "incomplete":
            return "reviewer_incomplete"
        if self.result is None:
            return "reviewer_failed"
        return self.result.verdict

    def log_record(self) -> dict:
        return {"provider": self.provider, "model": self.model, "reasoning_effort": self.effort,
                "attempts": self.attempts, "latency_s": self.latency_s, "cost": self.cost.as_dict(),
                "status": self.status, "verdict": self.verdict_label, "finish_reasons": self.finish_reasons,
                "max_tokens": MAX_TOKENS, "reviewer_version": self.version, "test_context": self.test_context,
                "result": self.result.model_dump() if self.result else None, "error": self.error,
                "reasoning_content": self.reasoning, "raw_content": self.raw}


def review(diff: str, guard_items: list[str], spec: DriftSpec, changelog: str | None, *,
           provider: str = "baseten", effort: str = DEFAULT_EFFORT, client=None,
           log_path: Path | None = None, task_id: str | None = None,
           test_context: tuple[str, list[str]] | None = None) -> ReviewOutcome:
    """`test_context` is (source text, test names) from `covering_tests`."""
    model = REVIEWERS[provider]
    client = client or providers.get_client(provider)
    ctx_text, ctx_names = test_context or (None, [])
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_user_prompt(diff, guard_items, spec, changelog, ctx_text)}]
    total = Cost()
    out = ReviewOutcome(result=None, provider=provider, model=model, effort=effort, cost=total, latency_s=0.0,
                        attempts=0, test_context=list(ctx_names))
    t0 = time.monotonic()
    for attempt in (1, 2):
        out.attempts = attempt
        r = client.chat.completions.create(model=model, messages=messages, max_tokens=MAX_TOKENS,
                                           response_format={"type": "json_object"},
                                           extra_body={"reasoning_effort": effort})
        msg = r.choices[0].message
        content = msg.content or ""
        out.raw.append(content)
        out.reasoning.append(getattr(msg, "reasoning_content", None) or "")
        u = r.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        reasoning = getattr(getattr(u, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
        c = Cost(u.prompt_tokens, cached, u.completion_tokens, reasoning,
                 usd(model, u.prompt_tokens, cached, u.completion_tokens))
        total = total + c
        finish = r.choices[0].finish_reason or ""
        out.finish_reasons.append(finish)
        if finish == "length":
            # Ran out of budget: record it, never read it as an approval, and don't burn a second budget.
            out.status = "incomplete"
            try:
                out.result = ReviewResult.model_validate_json(_strip_fences(content))
            except (ValidationError, ValueError):
                out.result = None
            out.error = f"attempt {attempt}: finish_reason=length after {u.completion_tokens} tokens (max {MAX_TOKENS})"
            break
        try:
            out.result = ReviewResult.model_validate_json(_strip_fences(content))
            out.error = None
            out.status = "ok"
            break
        except (ValidationError, ValueError) as exc:
            out.status = "failed"
            out.error = f"attempt {attempt}: {str(exc)[:500]} (finish_reason={r.choices[0].finish_reason})"
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": "That reply was not a valid JSON object for the required schema: "
                          f"{str(exc)[:300]}. Reply again with only the JSON object."}]
    out.cost = total
    out.latency_s = round(time.monotonic() - t0, 1)
    if log_path:
        with Path(log_path).open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": "review", "task_id": task_id, **out.log_record()}) + "\n")
    return out


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return m.group(1) if m else text.strip()


# --------------------------------------------------------------------------- unchanged tests covering changed code

def diff_old_touched(diff: str) -> dict[str, set[int]]:
    """file -> old-side line numbers that were removed, or where lines were inserted."""
    out: dict[str, set[int]] = {}
    path, old = None, 0
    for raw in diff.splitlines():
        if raw.startswith("--- "):
            p = raw[4:]
            path = None if p == "/dev/null" else p.removeprefix("a/")
            if path is not None:
                out.setdefault(path, set())
        elif raw.startswith("+++ "):
            continue
        elif raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)", raw)
            old = int(m.group(1)) if m else 0
        elif path is None or raw.startswith(("diff ", "index ", "new file", "deleted file", "\\")):
            continue
        elif raw.startswith("-"):
            out[path].add(old)
            old += 1
        elif raw.startswith("+"):
            out[path].add(old)  # insertion point
        else:
            old += 1
    return out


def _numeric_equality_asserts(src: str) -> int:
    """Asserts of the form `x == 87.0` (a numeric literal compared with ==, not via approx)."""
    import ast

    def is_num(n: ast.AST) -> bool:
        if isinstance(n, ast.UnaryOp):
            n = n.operand
        return isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare):
            cmp = node.test
            if any(isinstance(op, ast.Eq) for op in cmp.ops) and any(is_num(x) for x in [cmp.left, *cmp.comparators]):
                count += 1
    return count


def covering_tests(br, diff: str, project_rel: str, base_source, *, cap: int = TEST_CONTEXT_MAX_LINES
                   ) -> tuple[str, list[str]]:
    """Source of unchanged test functions that reach a modified library function in the blast-radius link graph.

    `br` is the BlastRadius of the project at the base commit (its line numbers are base lines), `base_source(path)`
    returns a project-relative file's base text. Tests with exact numeric equality asserts come first; total <= cap lines.
    """
    from collections import defaultdict, deque

    from driftzero.blast_radius import PROPAGATING
    from driftzero.guard import _is_test_path

    prefix = "" if project_rel in (".", "") else project_rel.rstrip("/") + "/"
    touched = {p[len(prefix):]: lines for p, lines in diff_old_touched(diff).items() if p.startswith(prefix)}

    def modified(sym) -> bool:
        return any(sym.line <= n <= sym.end_line for n in touched.get(sym.file, ()))

    changed_lib = {s.id for s in br.symbols if s.kind != "test" and not _is_test_path(s.file) and modified(s)}
    adj: dict[str, set[str]] = defaultdict(set)
    for e in br.edges:
        if e.kind in PROPAGATING:
            adj[e.src].add(e.dst)
    picked = []
    for t in (s for s in br.symbols if s.kind == "test"):
        seen, queue = {t.id}, deque([t.id])
        while queue:
            for nxt in adj[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if seen & changed_lib and not modified(t):
            text = base_source(t.file)
            if text is None:
                continue
            src = "\n".join(text.splitlines()[t.line - 1:t.end_line])
            picked.append((t, src))
    picked.sort(key=lambda ts: (-_numeric_equality_asserts(ts[1]), ts[0].file, ts[0].line))
    blocks, names, used = [], [], 0
    for t, src in picked:
        n = src.count("\n") + 2
        if used + n > cap:
            continue
        blocks.append(f"# {t.file}::{t.name}\n{src}")
        names.append(f"{t.file}::{t.name}")
        used += n
    return "\n\n".join(blocks), names


# --------------------------------------------------------------------------- mapping findings onto the diff

def diff_new_lines(diff: str) -> dict[str, dict[int, str]]:
    """file -> {new-side line number: line text} for added and context lines (what GitHub can comment on)."""
    out: dict[str, dict[int, str]] = {}
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:]
            path = None if p == "/dev/null" else p.removeprefix("b/")
            if path is not None:
                out.setdefault(path, {})
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif path is None or raw.startswith(("--- ", "diff ", "index ", "new file", "deleted file", "similarity",
                                              "rename ", "\\")):
            continue
        elif raw.startswith("+"):
            out[path][line] = raw[1:]
            line += 1
        elif raw.startswith("-"):
            continue
        else:
            out[path][line] = raw[1:] if raw.startswith(" ") else raw
            line += 1
    return out


def _match_file(name: str, files: list[str]) -> str | None:
    if name in files:
        return name
    cands = [f for f in files if f.endswith("/" + name.lstrip("./")) or name.endswith("/" + f)]
    return cands[0] if len(cands) == 1 else None


def locate(f: Finding, lines: dict[str, dict[int, str]]) -> tuple[str, int] | None:
    """(path, new-side line) for an inline comment, or None to put the finding in the review body."""
    path = _match_file(f.file, list(lines))
    if path is None:
        return None
    m = re.search(r"\d+", f.line_hint or "")
    if m and int(m.group()) in lines[path]:
        return path, int(m.group())
    for ev in f.evidence.splitlines():
        ev = ev.strip()
        if not ev.startswith("+") or len(ev.strip("+ ")) < 4:
            continue
        needle = ev[1:].strip()
        for n, text in sorted(lines[path].items()):
            if needle and needle in text.strip():
                return path, n
    return None


def _finding_md(f: Finding, with_location: bool = True) -> str:
    where = f"`{f.file}`" + (f" (line {f.line_hint})" if f.line_hint else "") if with_location else ""
    ev = "\n".join("    " + ln for ln in f.evidence.splitlines())
    return (f"**{f.severity} · {f.category}** {where}\n\n{f.explanation}\n\n{ev}\n\n"
            f"Suggested fix: {f.suggested_fix}")


def post_review(slug: str, pr_number: int | str, outcome: ReviewOutcome, diff: str, *, strict: bool = False,
                cwd: Path | None = None) -> dict:
    """Post the result as a GitHub PR review: inline where line_hint maps to the diff, the rest in the body."""
    res = outcome.result
    lines = diff_new_lines(diff)
    inline, body_only = [], []
    for f in (res.findings if res else []):
        loc = locate(f, lines)
        if loc:
            inline.append({"path": loc[0], "line": loc[1], "side": "RIGHT", "body": _finding_md(f, with_location=False)})
        else:
            body_only.append(f)
    high = any(f.severity == "high" for f in (res.findings if res else []))
    event = "REQUEST_CHANGES" if (strict and high) else "COMMENT"
    head = [f"### {REVIEWER_LABEL} (`{outcome.model}` on {outcome.provider.title()}, reasoning_effort={outcome.effort})",
            ""]
    if outcome.status == "incomplete":
        head.append(f"**reviewer_incomplete**: the model hit max_tokens={MAX_TOKENS} before finishing "
                    f"({outcome.cost.output_tokens:,} output tokens). This is not an approval.")
    elif res is None:
        head.append(f"Review failed: {outcome.error}")
    else:
        head.append(f"Verdict: **{res.verdict}** · {len(res.findings)} finding(s), {len(inline)} inline · "
                    f"${outcome.cost.usd or 0:.4f}, {outcome.latency_s:.0f} s")
    if body_only:
        head += ["", "Findings without a matching diff line:", ""] + [_finding_md(f) for f in body_only]
    payload = {"body": "\n".join(head), "event": event, "comments": inline}

    def call(p: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", "api", "-X", "POST", f"repos/{slug}/pulls/{pr_number}/reviews", "--input", "-"],
                              input=json.dumps(p), cwd=cwd, capture_output=True, text=True)

    r = call(payload)
    if r.returncode != 0 and event == "REQUEST_CHANGES" and "own pull request" in (r.stdout + r.stderr):
        payload["event"] = "COMMENT"  # GitHub forbids requesting changes on your own PR
        payload["body"] += "\n\n_(Would request changes: a high-severity finding with --strict; GitHub does not allow " \
                           "that on your own pull request.)_"
        r = call(payload)
    if r.returncode != 0:
        raise RuntimeError(f"posting review failed: {r.stdout[-400:]} {r.stderr[-400:]}")
    data = json.loads(r.stdout)
    return {"event": payload["event"], "url": data.get("html_url"), "inline": len(inline), "body_only": len(body_only)}


def outcome_from_record(rec: dict) -> ReviewOutcome:
    c = rec.get("cost") or {}
    return ReviewOutcome(
        result=ReviewResult.model_validate(rec["result"]) if rec.get("result") else None,
        provider=rec["provider"], model=rec["model"], effort=rec.get("reasoning_effort", DEFAULT_EFFORT),
        cost=Cost(c.get("input_tokens", 0), c.get("cached_tokens", 0), c.get("output_tokens", 0),
                  c.get("reasoning_tokens", 0), c.get("usd")),
        latency_s=rec.get("latency_s", 0.0), attempts=rec.get("attempts", 1),
        reasoning=rec.get("reasoning_content") or [], raw=rec.get("raw_content") or [], error=rec.get("error"),
        status=rec.get("status") or ("ok" if rec.get("result") else "failed"),
        finish_reasons=rec.get("finish_reasons") or [], test_context=rec.get("test_context") or [],
        version=rec.get("reviewer_version", 1))


def latest_logged(paths: list[Path]) -> ReviewOutcome | None:
    """Most recent successful review recorded in any of these JSONL logs."""
    best = None
    for p in paths:
        if not Path(p).exists():
            continue
        for line in Path(p).read_text().splitlines():
            rec = json.loads(line)
            if rec.get("event") == "review" and (best is None or rec["ts"] > best["ts"]):
                best = rec
    return outcome_from_record(best) if best else None


def repo_slug(cwd: Path) -> str | None:
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- standalone

def inputs_for_task(home: Path, task_id: str):
    """(LoopResult, diff text, guard items, changelog text) for a finished run, from saved data."""
    from driftzero import pr as pr_mod
    from driftzero import repair as repair_mod

    res, _ = pr_mod.load_run(home, task_id)
    wt = res.worktree
    diff = subprocess.run(["git", "diff", wt.base, "HEAD"], cwd=wt.path, check=True, capture_output=True,
                          text=True).stdout or repair_mod.collect_diff(wt).patch
    guard_items = [str(f) for f in (res.guard.review_items if res.guard else [])]
    project = wt.project
    logs = sorted(project.glob("CHANGELOG*.md"))
    changelog = logs[0].read_text() if logs else None
    return res, diff, guard_items, changelog


def test_context_for(res, diff: str) -> tuple[str, list[str]]:
    """covering_tests() for a LoopResult: base-commit sources read from its worktree."""
    wt = res.worktree
    prefix = "" if wt.project_rel in (".", "") else wt.project_rel.rstrip("/") + "/"

    def base_source(rel: str) -> str | None:
        p = subprocess.run(["git", "show", f"{wt.base}:{prefix}{rel}"], cwd=wt.path, capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else None

    return covering_tests(res.blast_radius, diff, wt.project_rel, base_source)


if __name__ == "__main__":
    tid = sys.argv[1]
    prov = sys.argv[sys.argv.index("--provider") + 1] if "--provider" in sys.argv else "baseten"
    eff = sys.argv[sys.argv.index("--effort") + 1] if "--effort" in sys.argv else DEFAULT_EFFORT
    home = Path(__file__).resolve().parents[1]
    res, d, g, cl = inputs_for_task(home, tid)
    ctx = test_context_for(res, d)
    print(f"test context: {len(ctx[1])} tests, {ctx[0].count(chr(10)) + 1} lines: {ctx[1]}", file=sys.stderr)
    o = review(d, g, res.spec, cl, provider=prov, effort=eff, log_path=home / ".driftzero" / "runs" / f"{tid}.review.jsonl",
               task_id=tid, test_context=ctx)
    print(json.dumps({"verdict": o.verdict_label, "status": o.status, "finish_reasons": o.finish_reasons,
                      "error": o.error, "attempts": o.attempts,
                      "latency_s": o.latency_s, "cost": o.cost.as_dict(),
                      "findings": [f.model_dump() for f in (o.result.findings if o.result else [])]}, indent=2))
