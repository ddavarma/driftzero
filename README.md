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
`--review-fix` adds one reviewer-driven repair iteration when a finding has severity ≥ medium and confidence ≥
`--review-fix-min-confidence` (default 0.6); `driftzero review-fix --task <id>` runs that pass on a finished run.
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
DriftSpec. `charging.py` is a control module the change must not touch. `holdout_v2/` has 8 contract tests derived
only from the v2 spec and changelog. They are never shown to the agent, excluded from the blast radius, and run with
`--noconftest -c /dev/null` so nothing in the worktree can influence them.

## Results so far

| run | iterations | guard | agent suite | hold-out | sandbox | cost | wall time |
|---|---|---|---|---|---|---|---|
| [changelog → PR #5 on fleet-telemetry-v1, `--review-fix`, shipped reviewer (v2.1 + filter)](https://github.com/ddavarma/fleet-telemetry-v1/pull/5) (`20261030-161858-a70fa1`) | 1, then a manual review-fix pass at lowered thresholds (committed `c9d2e8e`) | ok, 14 review items (both passes) | 23/23, re-verify 23/23 | 8/8, re-verify 8/8 | Modal, 3.7 s + 5.4 s | $0.85 ($0.56 run + $0.23 fix pass + $0.056 second review) | 234 s + 99 s for the fix pass |
| [changelog → PR #4 on fleet-telemetry-v1, `--review-fix`, reviewer v2.2](https://github.com/ddavarma/fleet-telemetry-v1/pull/4) (`20261030-155223-26855b`) | **2**: iteration 1 failed the new health-range hold-out (7/8), the redacted retry fixed it | ok, 13 review items | 22/22 | 8/8 | Modal, 4.5 s + 5.2 s | $0.68 (differ $0.026, review $0.040) | 234 s end to end |
| [changelog → PR #3 on fleet-telemetry-v1, `--review-fix`, reviewer v2.1](https://github.com/ddavarma/fleet-telemetry-v1/pull/3) (`20261030-043600-9b46d7`) | 1 + 1 review-fix (no code change: all 4 findings justified) | ok, 15 review items | 23/23, re-verify 23/23 | 7/7, re-verify 7/7 | Modal, 4.7 s + 6.6 s | $0.84 (differ $0.019, review $0.065, fix pass $0.23) | 300 s end to end |
| [changelog → PR #2 on fleet-telemetry-v1, `--review-fix`](https://github.com/ddavarma/fleet-telemetry-v1/pull/2) (`20261030-041743-ffdf04`) | 1 (+0: reviewer approved, no fix pass) | ok, 14 review items | 25/25 | 7/7 | Modal, 3.8 s | $0.67 (differ $0.026, review $0.068) | 260 s end to end |
| [changelog → PR on fleet-telemetry-v1](https://github.com/ddavarma/fleet-telemetry-v1/pull/1) (`20261030-031042-391b51`) | 1 | ok; flagged `yaw == 87.0` loosened to `approx`, stale test name | 25/25 | 7/7 | Modal, 4.1 s | $0.59 (incl. $0.024 differ) | 175 s + 18 s differ |
| [`driftzero migrate`, local spec](https://github.com/ddavarma/driftzero/pull/1) (`20261030-023010-e61999`, draft, evidence only) | 1 | ok, 0 rejections, 17 review items | 30/30 | 7/7 | Modal, 7.3 s | $0.37 (986K in, 91% cached, 28K out) | 142 s end to end |
| stage-4 repair + stage-5 verify (`20261030-014828-02a484`) | 1 | ok, 10 review items (guard run afterwards) | 21/21 | 7/7 | Modal, 13.4 s | $0.46 | 122 s repair |

The differ's output on the benchmark matches the hand-written `driftspec_v2.json` in every category (endpoints,
removed/added fields with their replacements, type and value changes), with the changelog's formulas attached to
the right entries ([`docs/diff/driftspec_v2.generated.json`](docs/diff/driftspec_v2.generated.json)).

Logs: [`docs/runs/`](docs/runs/). The end-to-end explanation, with every number checked against these logs, is in
[`docs/PRODUCT.md`](docs/PRODUCT.md). Muse's event vocabulary: [`docs/muse_events.md`](docs/muse_events.md).

## External test: JobTracker (OpenAI SDK v0→v1)

The first run on a repository we did not write. Target: [resumejob/JobTracker](https://github.com/resumejob/JobTracker)
(MIT, 607 Python lines, 5 pytest tests that mock the v0 API and pass offline with `openai==0.28.1`), forked to
[ddavarma/JobTracker-driftzero-test](https://github.com/ddavarma/JobTracker-driftzero-test). Drift input: the official
v1.0.0 migration guide, quoted verbatim in
[`benchmarks/jobtracker_openai_v1/CHANGELOG_openai_v1.md`](benchmarks/jobtracker_openai_v1/CHANGELOG_openai_v1.md); no
OpenAPI documents exist. 7 hold-outs (respx-mocked v1 HTTP calls) failed 7/7 on the v0 code and passed 7/7 on a
hand-written migration against `openai` 1.109.1.

**Run `20261030-165401-ebb8d2`: not verified, no PR.** 3 iterations, $1.15, 413 s. What happened, in order:

- **Differ** ($0.049): with no OpenAPI to diff, all 60 DriftSpec entries came from the model, forced into "fields" of
  a schema named `openai` (`ChatCompletion.create` → `chat.completions.create` as a rename). It repeated the guide's
  claim that `openai.OpenAIError` was removed, which is false for the real v1 SDK.
- **Blast radius**: 3 symbols in 1 file, **0 tests**. It matched bare attribute names (`create`, `list`), and missed
  the tests (they reach the code through `unittest` `setUp`), the `@patch('openai.Model.list')` strings and the
  dict-style response access.
- **Repair**: Muse migrated the code to the v1 client with attribute access in iteration 1. The guard passed every
  iteration (0 rejections, 4 restructured tests reported).
- **Verify, iteration 1**: the repo has no `pyproject.toml`/`setup.py`, so `pip install -e` failed and `openai` was
  never installed in the sandbox; both suites failed at import.
- **Muse's response**: given the agent-suite traceback (`No module named 'openai'`), it added a `pyproject.toml`
  (`openai>=1.0.0`) **and wrapped the import in `try: from openai import OpenAI, OpenAIError, BadRequestError`
  `except ImportError:` with stub `OpenAI`/`OpenAIError`/`BadRequestError` classes**, so the library imports without
  the SDK installed. That is library code shaped around a broken test environment, and the guard, which only inspects
  tests, did not see it.
- **Verify, iterations 2–3**: the agent's 5 tests passed, but the hold-outs never ran: `ModuleNotFoundError: No module
  named 'httpx'`. Reproduced outside DriftZero ([`docs/runs/20261030-165401-ebb8d2.diag_sandbox.py`](docs/runs/)):
  `openai>=1.0.0` resolved to **openai 3.41.0**, which uses `httpx2`, and verify never installed the hold-outs'
  dependencies (`respx`). Both were gaps: DriftZero's, and the hold-outs', which had only been validated against 1.x.
- **Retry feedback**: the redacted hold-out failure was just "collection failure", which Muse could not act on.

Logs: [`docs/runs/20261030-165401-ebb8d2.*`](docs/runs/).

**Rerun `20261030-171945-fe1c00` after fixing those gaps: verified in 1 iteration,
[PR #1 on the fork](https://github.com/ddavarma/JobTracker-driftzero-test/pull/1).** Changes since the failed run:
verify installs the target's declared dependencies plus the target pin and the hold-outs' own requirements (from
`benchmarks/targets.json`); `DriftSpec.target_dependency` (`--target-dep "openai>=1,<2"`; the guide states no
constraint) is pinned in the repair prompt and installed by verify, and the hold-outs were re-validated against it;
Muse runs its tests in a per-run venv with the target's dependencies instead of DriftZero's own; the guard rejects
library code that stubs the target dependency; collection errors reach the retry prompt with hold-out paths scrubbed;
blast radius follows `mock.patch` strings and `unittest` `setUp`; and benchmark-specific repair rules (the fleet
telemetry ones had been sent to JobTracker too) moved into per-target config.

- Differ $0.050 (59 entries, again all from the changelog). Blast radius: 11 symbols in 3 files, 3 tests (was 3
  symbols, 0 tests). Repair: 23 steps. `requirements.txt` `openai==0.28.1` → `openai<2,>=1`; an explicit
  `OpenAI(api_key=...)` client, `client.models.list()` with `.id`, `client.chat.completions.create`,
  `message.function_call.arguments`, `openai.OpenAIError`; no stubs, `tiktoken`/`requests` imports intact.
- Guard ok: 0 rejections, the 4 restructured mocks reported for review. Modal: install of `requirements.txt` + pin
  + `respx` all exit 0; agent suite 5/5, hold-outs 7/7.
- Reviewer: approve, one low-confidence finding (0.35: an unreachable pre-existing `except json.JSONDecodeError`); no
  fix pass. $0.40 total, 173 s end to end.
- Logs: [`docs/runs/20261030-171945-fe1c00.*`](docs/runs/).

## Limitations

- **The open-weights reviewer needed the right inputs to catch the rounding.** Same model (Muse Spark Open on
  Baseten, `reasoning_effort=high`), same two diffs, prompt never mentions rounding (a test pins that):
  - *v1* (diff + guard items + DriftSpec + changelog, 8,000 tokens): **approved both PRs with no findings.** On
    DriftZero PR #1 its reasoning asked "Could there be magic constants like rounding to 3 decimals to make test
    pass?", then accepted Muse's code comment ("quoted to 1e-3 deg (v1 wire resolution)") as if the spec said so. The
    test the rounding serves (`frame.pose.yaw == 87.0`) is unchanged, so it was not in the diff. At `xhigh` it spent
    the whole 8,000-token budget reasoning and returned nothing, twice.
  - *v2* adds the unchanged tests that reach modified library code (from the blast-radius link graph), a 24,000-token
    budget with `finish_reason=length` reported as `reviewer_incomplete`, and a rule that code comments are the
    agent's claims to be checked against the DriftSpec and changelog. Measured over a clean batch of 3 runs per PR
    ([`docs/reviewer_eval.md`](docs/reviewer_eval.md)): it reported the rounding on DriftZero PR #1 in **3/3** runs
    (2 as `test_shaped_code`, 1 as `precision_loss`; severity medium or low), and the required-fields-with-defaults
    issue in **3/3** runs there but **0/3** on fleet-telemetry-v1 PR #1, where it approved with no findings every
    time. Counting every v2 run on those two PRs: rounding 7/7, defaults 6/7 and 2/6. **On a fresh diff it missed
    it:** fleet-telemetry-v1 PR #2 rounds the Euler angles again (with a more plausible "~6-decimal quaternions"
    comment); v2 drafted the finding in its reasoning, then approved citing the prompt's "report nothing you are unsure
    is real" rule, so `--review-fix` had nothing to act on.
  - *v2.1* changes only the abstention policy: that rule is gone, every finding carries a `confidence`, suppression is
    forbidden ("low confidence is" allowed), and the verdict is `approve` iff nothing reaches 0.5. Over 9 runs
    ([`docs/reviewer_eval.md`](docs/reviewer_eval.md)) it reported the rounding **3/3** on both diffs that contain it
    (at ≥ 0.6 confidence 3/3 and 2/3) and the defaults issue **3/3** on both diffs that contain it, with no false
    defaults finding on the diff without them. **It is also noisier**: 5–8 findings per run instead of 0–3, severity
    inflation (requested design choices called high), and on fleet-telemetry-v1 PR #1, hand-checked against the code,
    14 non-defaults findings were 4 reports of 3 real unlabelled defects (health range not validated, a
    quaternion-constructed `Pose` leaving its Euler fields at 0.0, a bare `ValueError` escaping the parser), 4
    observations of requested design choices, 4 debatable, and 2 spurious (one accused a comment of an unsupported
    claim that is actually in `openapi_v2.json`, which the reviewer is not given). v2 misses things; v2.1 finds them
    and also invents some. Its confidence scores carry signal but are not calibrated.
  - *v2.2* also gives the reviewer the repair agent's hard rules as "agreed migration policy" (don't report what the
    policy requires; do report violations and gaps). Over 9 runs it removed the noise: on fleet-telemetry-v1 PR #1,
    10 non-defaults findings were 8 real reports (4 distinct defects), 2 debatable, 0 spurious, 0 complaints about the
    requested compatibility layer. **But the catch rate went down**: the PR #2 rounding was mentioned in 2/3 runs and
    never reached 0.5 (v2.1: 2/3 at ≥ 0.6), with the reviewer reasoning that the policy's permission to keep derived
    Euler values covers rounding them; the defaults finding fell to 1/3 (DriftZero PR #1) and 2/3 (fleet-telemetry-v1
    PR #1) at ≥ 0.5.
  - *v2.3* adds one sentence to the policy: it governs which interfaces exist, not how values are computed;
    numeric fidelity to the DriftSpec and changelog formulas is always in scope. **The catch rate went down again**:
    over 9 runs no finding reached 0.5 confidence (highest 0.45), so every run approved and `--review-fix` would never
    trigger. The known issues are still mentioned at low confidence (rounding 3/3 and 2/3, defaults 3/3 and 2/3), and
    on fleet-telemetry-v1 PR #1 7 of 9 non-defaults findings were real defects, 1 spurious. At ≥ 0.6 the rounding was
    caught in 5/6 runs by v2.1, 2/6 by v2.2 and 0/6 by v2.3.
  - **What ships (frozen): the v2.1 prompt, byte-for-byte, plus a deterministic post-filter.** Policy left the prompt
    because both prompt-level versions cost recall. The filter tags findings about compatibility code the repair rules
    require (category `dead_compat_code`, or evidence that only declares a removed v1 field kept as a derived value) as
    `policy-required`: shown in the PR, never a `--review-fix` trigger. Replayed on the stored v2.1 eval findings it
    tags 14 of 53 and keeps the rounding in the trigger set exactly as often as raw v2.1 (3/3 and 2/3); the cost is that
    a defaults finding the model happens to label `dead_compat_code` is excluded too. All numbers:
    [`docs/reviewer_eval.md`](docs/reviewer_eval.md).
  - Caveats: 3 runs per cell, one model, one effort level, two diffs; no false-positive measurement. The v2 design
    (test context, claims rule) was chosen after seeing v1 miss this case. The reviewer's own `line_hint`s are
    unreliable (off by 1–12 lines), so inline comments are anchored by matching the quoted evidence to the diff.
- **Rounding-style smells pass.** Twice Muse rounded the derived Euler angles to 3 decimals so a pre-existing exact
  check (`yaw == 87.0`) kept passing. Nothing here catches library code bent to fit a test: the guard only inspects
  test files, and the hold-outs use tolerances. It shows up only in human review of the diff. (When Muse instead
  loosened the test to `pytest.approx`, the guard flagged it.)
- **Python only.** Blast radius, the guard and verification assume Python, `ast` and pytest. Impact analysis is
  static, with no type inference: method calls resolve by unique method name, and dynamic access (`getattr`,
  `**kwargs` payload plumbing) is invisible.
- **The shipped reviewer under-rates a real issue on a fresh diff.** On fleet-telemetry-v1 PR #5 it reported the
  required-fields-with-defaults issue (which is present) at 0.40, so the verdict was `approve` and the default trigger
  (severity ≥ medium, confidence ≥ 0.6) did not fire; its other three findings were `policy-required`. The fix path
  was then run manually with both thresholds lowered (`driftzero review-fix --review-fix-min-severity low
  --review-fix-min-confidence 0.3`, disclosed in the PR): Muse made the two fields required (commit `c9d2e8e`, two
  hunks in `models.py`), re-verified 23/23 and 8/8, and the second review approved without the defaults finding.
  That is the only live fix commit so far, and it needed lowered thresholds.
- **The repair brief and the reviewer disagree on compatibility.** In the live `--review-fix` run (fleet-telemetry-v1
  PR #3) the trigger fired on 4 findings (confidence 0.70–0.82), and Muse ran the extra iteration, re-verified (23/23,
  7/7), and **changed nothing**: it justified all four by citing the repair prompt's own rule to keep the Python API
  source-compatible, which is exactly what the reviewer flags as dead compat code. Nothing was pushed and no second
  review ran.
- **One scenario.** Everything is proven on one API change in one domain. The differ has only been checked against
  that one changelog; a second scenario (e.g. float dollars → integer cents) is not built yet.
- **Hold-outs need a mapping.** A remote target gets hold-out tests only if `benchmarks/targets.json` lists it.
  Without them verification does not run and the loop never reports success, so no PR is opened (fails closed).
- **Hold-outs are only as strong as they are written.** 8 tests, written by hand from the spec (the 8th, health range, was added after the reviewer found that two Muse repairs accepted `health=2.0`; it then caught that defect live in PR #4's first iteration). Earlier results in the table above ran against 7. After a failed
  iteration the agent sees their names and one-line messages, which is enough to target them.
- **Guard rules are syntactic.** It counts asserts and compares literals. It cannot tell a legitimately updated
  expected value from a wrong one (it reports them for review), and it does not police non-test code.
- **Muse's sandbox can't run `git` in a worktree of a clone** (`fatal: not a git repository`): the clone's git
  metadata sits outside what the sandbox lets it read. Harmless, since the agent is told not to touch git and
  DriftZero commits outside the sandbox.
- Costs use Meta's published rates as of 2026-09 (`driftzero/telemetry.py`); verify them at dev.meta.ai/docs/models.
