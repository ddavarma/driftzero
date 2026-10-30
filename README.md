# DriftZero

Autonomous API-drift repair agent. Built during the Meta Global AI Developer Hackathon, Oct 26–Nov 4 2026.

An upstream API ships a breaking change. DriftZero maps the code it breaks, has Meta's Muse Code repair it in an
isolated git worktree, rejects repairs that cheat by editing tests, proves the result in a Modal sandbox against
hold-out contract tests the agent never saw, and opens a GitHub pull request with the evidence attached.

```
driftzero migrate --repo https://github.com/ddavarma/fleet-telemetry-v1 \
    --changelog CHANGELOG_v2.md --openapi-v1 openapi_v1.json --openapi-v2 openapi_v2.json
```

## Pipeline

```
 changelog.md, openapi_v1.json, openapi_v2.json        (or a hand-written --spec)
      │
      ▼
 differ            driftzero/differ.py         deterministic OpenAPI JSON diff → candidate changes, then ONE
      │                                         muse-spark-1.3 call (strict json_schema output) that annotates them
      │                                         with replacements and migration formulas from the changelog; it
      │                                         may add what only the changelog states, never drop or re-shape
      ▼
 DriftSpec (JSON)
      │
      ▼
 target            driftzero/target.py         local path, or a git URL cloned to .driftzero/clones/<id>
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
 pull request      driftzero/pr.py             commit, push driftzero/<id> to the target's origin, gh pr create -R:
      │                                         lead box, agent summary, review findings, DriftSpec, blast radius
      │                                         with test coverage, test changes, hold-out results, cost, logs
      ▼
 code review       driftzero/reviewer.py       Muse Spark Open (open weights) on Baseten (or --reviewer-provider
                                                fireworks), JSON mode, told what the guard already checks; looks for
                                                test-shaped code, precision changes, unrequested behaviour changes,
                                                compat paths. Posted as a PR review (inline where the line maps) and
                                                added to "Review findings" with its source. --no-review skips it.
```

Every stage also runs on its own: `driftzero diff`, `driftzero blast`, `driftzero repair [--dry-run]`, `driftzero verify [--local]`,
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
driftzero diff    --changelog $B/CHANGELOG_v2.md --openapi-v1 $B/openapi_v1.json --openapi-v2 $B/openapi_v2.json \
                  --out spec.json --compare $B/driftspec_v2.json     # changelog -> DriftSpec, ~$0.02
driftzero blast   --repo $B --spec $B/driftspec_v2.json             # impact table, no LLM
driftzero repair  --repo $B --spec $B/driftspec_v2.json --dry-run   # plumbing via Muse's echo provider, $0
driftzero migrate --repo $B --spec $B/driftspec_v2.json --no-pr     # full run, push branch only
driftzero migrate --repo $B --spec $B/driftspec_v2.json             # full run + pull request (on this repo)

# a remote target, straight from the changelog: clone, repair, verify, PR on the target
driftzero migrate --repo https://github.com/ddavarma/fleet-telemetry-v1 \
    --changelog $B/CHANGELOG_v2.md --openapi-v1 $B/openapi_v1.json --openapi-v2 $B/openapi_v2.json

driftzero pr-body --task <id> [--update <pr>]                         # re-render a PR body from a saved run
```

`driftzero review --task <id> [--pr <url>] [--effort high|xhigh] [--reuse]` runs the reviewer on a finished run.
`--strict` posts REQUEST_CHANGES when a finding is high severity (GitHub refuses that on your own PR; DriftZero then
posts a COMMENT and says so). `--contributor` uses Meta's contributor tier (about 12x cheaper, but Meta trains on it; for development only).
`--local` verifies in a temp copy instead of Modal. `--max-iterations N` caps the repair/verify loop.

## The benchmark

The demo target is a separate public repo, [ddavarma/fleet-telemetry-v1](https://github.com/ddavarma/fleet-telemetry-v1),
which holds only the library, its tests, the two OpenAPI documents and the changelog. Its hold-out tests stay here;
[`benchmarks/targets.json`](benchmarks/targets.json) maps `owner/name` to them. The same code lives in
`benchmarks/robotics_telemetry_drift/` for DriftZero's offline tests. It is a small fleet-telemetry client library (dataclasses + NumPy, 7 modules,
18 tests) written against API v1: Euler roll/pitch/yaw in degrees and a scalar position `noise`. API v2
(`openapi_v2.json`, `CHANGELOG_v2.md`) switches to unit quaternions (sign not canonicalised, two robots have
`qw < 0`), a 6×6 pose covariance, a required health score, and `/v2` paths. `driftspec_v2.json` is the hand-written
DriftSpec. `charging.py` is a control module the change must not touch. `holdout_v2/` has 7 contract tests derived
only from the v2 spec and changelog. They are never shown to the agent, excluded from the blast radius, and run with
`--noconftest -c /dev/null` so nothing in the worktree can influence them.

## Results so far

| run | iterations | guard | agent suite | hold-out | sandbox | cost | wall time |
|---|---|---|---|---|---|---|---|
| [changelog → PR on fleet-telemetry-v1](https://github.com/ddavarma/fleet-telemetry-v1/pull/1) (`20261030-031042-391b51`) | 1 | ok; flagged `yaw == 87.0` loosened to `approx`, stale test name | 25/25 | 7/7 | Modal, 4.1 s | $0.59 (incl. $0.024 differ) | 175 s + 18 s differ |
| [`driftzero migrate`, local spec](https://github.com/ddavarma/driftzero/pull/1) (`20261030-023010-e61999`, draft, evidence only) | 1 | ok, 0 rejections, 17 review items | 30/30 | 7/7 | Modal, 7.3 s | $0.37 (986K in, 91% cached, 28K out) | 142 s end to end |
| stage-4 repair + stage-5 verify (`20261030-014828-02a484`) | 1 | ok, 10 review items (guard run afterwards) | 21/21 | 7/7 | Modal, 13.4 s | $0.46 | 122 s repair |

The differ's output on the benchmark matches the hand-written `driftspec_v2.json` in every category (endpoints,
removed/added fields with their replacements, type and value changes), with the changelog's formulas attached to
the right entries ([`docs/diff/driftspec_v2.generated.json`](docs/diff/driftspec_v2.generated.json)).

Logs: [`docs/runs/`](docs/runs/). Muse's event vocabulary: [`docs/muse_events.md`](docs/muse_events.md).

## Limitations

- **The open-weights reviewer did not catch the rounding.** Run unaided on DriftZero PR #1, Muse Spark Open
  (`reasoning_effort=high`) approved with no findings. Its reasoning did ask "Could there be magic constants like
  rounding to 3 decimals to make test pass?", then accepted Muse's code comment ("quoted to 1e-3 deg (v1 wire
  resolution)") and asserted the spec says so, which it does not. At `xhigh` it got closer ("Why 3? ... no spec
  mentions. Could be test_shaped...") but spent all 8,000 tokens reasoning and returned nothing, twice. The test the
  rounding serves (`frame.pose.yaw == 87.0`) is unchanged, so it is not in the diff the reviewer sees. Logs:
  [`docs/runs/*.review.jsonl`](docs/runs/). It also did not repeat the guard's `approx` finding on fleet-telemetry-v1,
  as instructed.
- **Rounding-style smells pass.** Twice Muse rounded the derived Euler angles to 3 decimals so a pre-existing exact
  check (`yaw == 87.0`) kept passing. Nothing here catches library code bent to fit a test: the guard only inspects
  test files, and the hold-outs use tolerances. It shows up only in human review of the diff. (When Muse instead
  loosened the test to `pytest.approx`, the guard flagged it.)
- **Python only.** Blast radius, the guard and verification assume Python, `ast` and pytest. Impact analysis is
  static, with no type inference: method calls resolve by unique method name, and dynamic access (`getattr`,
  `**kwargs` payload plumbing) is invisible.
- **One scenario.** Everything is proven on one API change in one domain. The differ has only been checked against
  that one changelog; a second scenario (e.g. float dollars → integer cents) is not built yet.
- **Hold-outs need a mapping.** A remote target gets hold-out tests only if `benchmarks/targets.json` lists it.
  Without them verification does not run and the loop never reports success, so no PR is opened (fails closed).
- **Hold-outs are only as strong as they are written.** 7 tests, written by hand from the spec. After a failed
  iteration the agent sees their names and one-line messages, which is enough to target them.
- **Guard rules are syntactic.** It counts asserts and compares literals. It cannot tell a legitimately updated
  expected value from a wrong one (it reports them for review), and it does not police non-test code.
- **Muse's sandbox can't run `git` in a worktree of a clone** (`fatal: not a git repository`): the clone's git
  metadata sits outside what the sandbox lets it read. Harmless, since the agent is told not to touch git and
  DriftZero commits outside the sandbox.
- Costs use Meta's published rates as of 2026-09 (`driftzero/telemetry.py`); verify them at dev.meta.ai/docs/models.
