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
MAX_TOKENS = 8000
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

Rules: base every finding on lines that appear in the diff and quote them verbatim in `evidence`. Do not report
style or naming nits. Report nothing you are unsure is real; an empty findings list with verdict "approve" is a valid
answer. Use "request_changes" only if at least one finding is high severity.

Reply with ONE JSON object and nothing else, matching exactly:
{SCHEMA_TEXT}"""


def build_user_prompt(diff: str, guard_items: list[str], spec: DriftSpec, changelog: str | None) -> str:
    return "\n".join([
        "## DriftSpec (the intended change)", "```json", spec.model_dump_json(indent=1), "```", "",
        "## Upstream changelog", changelog.strip() if changelog else "(not provided)", "",
        "## Already reported by the static guard (do not repeat)",
        *(f"- {g}" for g in guard_items), *([] if guard_items else ["(nothing)"]), "",
        "## Diff under review (library and tests)", "```diff", diff, "```", "",
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

    def log_record(self) -> dict:
        return {"provider": self.provider, "model": self.model, "reasoning_effort": self.effort,
                "attempts": self.attempts, "latency_s": self.latency_s, "cost": self.cost.as_dict(),
                "result": self.result.model_dump() if self.result else None, "error": self.error,
                "reasoning_content": self.reasoning, "raw_content": self.raw}


def review(diff: str, guard_items: list[str], spec: DriftSpec, changelog: str | None, *,
           provider: str = "baseten", effort: str = DEFAULT_EFFORT, client=None,
           log_path: Path | None = None, task_id: str | None = None) -> ReviewOutcome:
    model = REVIEWERS[provider]
    client = client or providers.get_client(provider)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_user_prompt(diff, guard_items, spec, changelog)}]
    total = Cost()
    out = ReviewOutcome(result=None, provider=provider, model=model, effort=effort, cost=total, latency_s=0.0,
                        attempts=0)
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
        try:
            out.result = ReviewResult.model_validate_json(_strip_fences(content))
            out.error = None
            break
        except (ValidationError, ValueError) as exc:
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
    if res is None:
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
        reasoning=rec.get("reasoning_content") or [], raw=rec.get("raw_content") or [], error=rec.get("error"))


def latest_logged(paths: list[Path]) -> ReviewOutcome | None:
    """Most recent successful review recorded in any of these JSONL logs."""
    best = None
    for p in paths:
        if not Path(p).exists():
            continue
        for line in Path(p).read_text().splitlines():
            rec = json.loads(line)
            if rec.get("event") == "review" and rec.get("result") and (best is None or rec["ts"] > best["ts"]):
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


if __name__ == "__main__":
    tid = sys.argv[1]
    prov = sys.argv[sys.argv.index("--provider") + 1] if "--provider" in sys.argv else "baseten"
    eff = sys.argv[sys.argv.index("--effort") + 1] if "--effort" in sys.argv else DEFAULT_EFFORT
    home = Path(__file__).resolve().parents[1]
    res, d, g, cl = inputs_for_task(home, tid)
    o = review(d, g, res.spec, cl, provider=prov, effort=eff, log_path=home / ".driftzero" / "runs" / f"{tid}.review.jsonl",
               task_id=tid)
    print(json.dumps({"verdict": o.result.verdict if o.result else None, "error": o.error, "attempts": o.attempts,
                      "latency_s": o.latency_s, "cost": o.cost.as_dict(),
                      "findings": [f.model_dump() for f in (o.result.findings if o.result else [])]}, indent=2))
