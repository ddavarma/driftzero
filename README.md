# DriftZero

Autonomous API-drift repair agent. Built during the Meta Global AI Developer Hackathon, Oct 26–Nov 4 2026.

An upstream API ships a breaking change. DriftZero maps the code it breaks, has Meta's Muse Code repair it in an
isolated git worktree, rejects repairs that cheat by editing tests, proves the result in a Modal sandbox against
hold-out contract tests the agent never saw, and opens a GitHub pull request with the evidence attached.

```
driftzero migrate --repo benchmarks/robotics_telemetry_drift --spec benchmarks/robotics_telemetry_drift/driftspec_v2.json
```

## Pipeline

```
 DriftSpec (JSON)                 changelog / OpenAPI v1 → v2, as structured data
      │
      ▼
 blast radius      driftzero/blast_radius.py   Python ast, no LLM: which fields, endpoints and
      │                                         constants are touched, then callers, fixtures and
      │                                         tests by import / call / fixture edges
      ▼
 worktree          driftzero/repair.py         git worktree .driftzero/wt-<id>, branch driftzero/<id>;
      │                                         hold-out tests hidden (skip-worktree)
      ▼
 ┌──► repair       driftzero/repair.py         muse exec --json --approval-mode never, muse-spark-1.3,
 │    │                                         schema-shaped answer; JSONL events streamed as a live trace
 │    ▼
 │   guard         driftzero/guard.py          AST diff of tests: reject removed tests, fewer asserts,
 │    │                                         skip/xfail, bare comparisons, new conftest, pytest config;
 │    │                                         report literal / fixture / tolerance edits for review
 │    ▼
 │   verify        driftzero/verify.py         Modal sandbox (debian_slim py3.11): pip install -e, then
 │    │                                         the agent's tests/ and the hold-out suite as separate runs
 │    │
 └────┤ fail (≤ 3 iterations): guard reasons, agent pytest output, and hold-out failures
      │                         redacted to test name + one-line message
      ▼ pass
 pull request      driftzero/pr.py             commit, push driftzero/<id>, gh pr create: summary, DriftSpec,
                                                blast radius, test changes for review, verification, cost, logs
```

Every stage also runs on its own: `driftzero blast`, `driftzero repair [--dry-run]`, `driftzero verify [--local]`,
and `python -m driftzero.<stage>`. Each run writes `.driftzero/runs/<id>.*`: the loop log with tokens and USD per
iteration, Muse's raw JSONL events, the prompt sent, diffs, verify JSON, JUnit XML and the PR body.

## Run it

Requirements: Python 3.11, [Muse Code](https://dev.meta.ai) 1.4.x on `PATH`, a Modal account (`modal token new`),
and `gh` logged in.

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]' -e benchmarks/robotics_telemetry_drift modal
cp .env.example .env          # META_API_KEY=… (never commit .env)

pytest -q                                   # DriftZero's own tests: offline, free
DRIFTZERO_LIVE=1 pytest -q -m live          # + one Meta call and one Modal sandbox

B=benchmarks/robotics_telemetry_drift
driftzero blast   --repo $B --spec $B/driftspec_v2.json             # impact table, no LLM
driftzero repair  --repo $B --spec $B/driftspec_v2.json --dry-run   # plumbing via Muse's echo provider, $0
driftzero migrate --repo $B --spec $B/driftspec_v2.json --no-pr     # full run, push branch only
driftzero migrate --repo $B --spec $B/driftspec_v2.json             # full run + pull request
```

`--contributor` uses Meta's contributor tier (about 12x cheaper, but Meta trains on it; for development only).
`--local` verifies in a temp copy instead of Modal. `--max-iterations N` caps the repair/verify loop.

## The benchmark

`benchmarks/robotics_telemetry_drift/` is a small fleet-telemetry client library (dataclasses + NumPy, 7 modules,
18 tests) written against API v1: Euler roll/pitch/yaw in degrees and a scalar position `noise`. API v2
(`openapi_v2.json`, `CHANGELOG_v2.md`) switches to unit quaternions (sign not canonicalised, two robots have
`qw < 0`), a 6×6 pose covariance, a required health score, and `/v2` paths. `driftspec_v2.json` is the hand-written
DriftSpec. `charging.py` is a control module the change must not touch. `holdout_v2/` has 7 contract tests derived
only from the v2 spec and changelog. They are never shown to the agent, excluded from the blast radius, and run with
`--noconftest -c /dev/null` so nothing in the worktree can influence them.

## Results so far

| run | iterations | guard | agent suite | hold-out | sandbox | cost | wall time |
|---|---|---|---|---|---|---|---|
| [`driftzero migrate`](https://github.com/ddavarma/driftzero/pull/1) (`20261030-023010-e61999`) | 1 | ok, 0 rejections, 17 review items | 30/30 | 7/7 | Modal, 7.3 s | $0.37 (986K in, 91% cached, 28K out) | 142 s end to end |
| stage-4 repair + stage-5 verify (`20261030-014828-02a484`) | 1 | ok, 10 review items (guard run afterwards) | 21/21 | 7/7 | Modal, 13.4 s | $0.46 | 122 s repair |

Logs: [`docs/runs/`](docs/runs/). Muse's event vocabulary: [`docs/muse_events.md`](docs/muse_events.md).

## Limitations

- **Rounding-style smells pass.** In the first live repair Muse rounded the derived Euler angles to 3 decimals so a
  pre-existing exact check (`yaw == 87.0`) kept passing. Nothing here catches library code bent to fit a test: the
  guard only inspects test files, and the hold-outs use tolerances. It shows up only in human review of the diff.
- **Python only.** Blast radius, the guard and verification assume Python, `ast` and pytest. Impact analysis is
  static, with no type inference: method calls resolve by unique method name, and dynamic access (`getattr`,
  `**kwargs` payload plumbing) is invisible.
- **One scenario.** Everything is proven on one benchmark and one hand-written DriftSpec. The LLM differ
  (changelog → DriftSpec) and a second domain are not built yet.
- **Hold-outs are only as strong as they are written.** 7 tests, written by hand from the spec. After a failed
  iteration the agent sees their names and one-line messages, which is enough to target them.
- **Guard rules are syntactic.** It counts asserts and compares literals. It cannot tell a legitimately updated
  expected value from a wrong one (it reports them for review), and it does not police non-test code.
- **The benchmark lives in this repo**, so DriftZero's PRs target this repository's `main`. Merging one would migrate
  the benchmark itself; they are evidence, not meant to be merged.
- Costs use Meta's published rates as of 2026-09 (`driftzero/telemetry.py`); verify them at dev.meta.ai/docs/models.
