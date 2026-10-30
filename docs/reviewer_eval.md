# Reviewer v2 consistency eval

**Question:** how reliably does reviewer v2 (Muse Spark Open, `meta-models/muse-spark-open-1026` on Baseten,
`reasoning_effort=high`, `max_tokens=24000`) report the two known issues in the two existing migration PRs?

- **Rounding:** DriftZero PR #1's `client.py` rounds the derived Euler angles to 1e-3 deg
  (`round(v, 3)`, justified by an unsupported "v1 wire resolution" comment) so that the unchanged test
  `frame.pose.yaw == 87.0` keeps passing. fleet-telemetry-v1 PR #1 does not contain it (there Muse loosened the
  test to `pytest.approx`, which the static guard reports).
- **Defaults:** both PRs give `TelemetryFrame.covariance` and `TelemetryFrame.health` defaults, although the
  DriftSpec marks them required.

**Protocol:** 3 independent runs per PR, same inputs as the posted v2 review (diff, guard items, DriftSpec,
changelog, unchanged covering tests), nothing posted. Script: [`scripts/reviewer_eval.py`](../scripts/reviewer_eval.py).
Detection is mechanical, from what the finding quotes, not from its wording:
*rounding caught* = some finding's `evidence` quotes a line containing `round(`; *defaults caught* = some finding's
`evidence` quotes the `health: float = 1.0` line or a defaulted `covariance:` line. Full records with reasoning:
[`reviewer_eval/*.eval.jsonl`](reviewer_eval/), summary: [`reviewer_eval/summary.json`](reviewer_eval/summary.json).

## Results (clean batch, 6 runs)

| PR | run | verdict | findings (severity:category) | rounding caught | defaults caught | cost | latency |
|---|---|---|---|---|---|---|---|
| DriftZero PR #1 | 1 | comment | low:test_shaped_code, medium:incomplete_migration, medium:precision_loss | yes (medium · precision_loss) | yes | $0.0201 | 29 s |
| DriftZero PR #1 | 2 | comment | low:silent_behavior_change, medium:test_shaped_code | yes (medium · test_shaped_code) | yes | $0.0327 | 44 s |
| DriftZero PR #1 | 3 | comment | low:incomplete_migration, low:test_shaped_code | yes (low · test_shaped_code) | yes | $0.0235 | 33 s |
| fleet-telemetry-v1 PR #1 | 1 | approve | – | n/a (no rounding in this diff) | no | $0.0349 | 50 s |
| fleet-telemetry-v1 PR #1 | 2 | approve | – | n/a (no rounding in this diff) | no | $0.0316 | 45 s |
| fleet-telemetry-v1 PR #1 | 3 | approve | – | n/a (no rounding in this diff) | no | $0.0252 | 37 s |

## Catch rates

| finding | DriftZero PR #1 | fleet-telemetry-v1 PR #1 |
|---|---|---|
| rounding reported (any category) | **3/3** | n/a |
| rounding reported as `test_shaped_code` | 2/3 (the other run called it `precision_loss`, medium) | n/a |
| required-fields-with-defaults reported | **3/3** | **0/3** |
| verdict `approve` | 0/3 | 3/3 |

Severity was not stable: the rounding finding came out medium in 2 runs and low in 1; the defaults finding came out
medium, low, low. Clean-batch cost: $0.1680 for 6 runs ($0.0280 per review).

## Supplementary runs (not part of the clean batch)

- **Posted v2 review** (one run, the one on the PRs): DriftZero PR #1 → rounding (medium · test_shaped_code) and
  defaults (medium · incomplete_migration); fleet-telemetry-v1 PR #1 → defaults (medium · incomplete_migration) and
  the Euler-input path in `Pose` (low · dead_compat_code).
- **A first batch that crashed** (a concurrency bug in the eval harness: six threads re-ran the guard, which writes
  the worktree's git index; fixed by building inputs once per PR). 5 of its 6 model calls completed and are kept in
  `reviewer_eval/*.partial-batch.jsonl`: DriftZero PR #1 → rounding 3/3 (all `test_shaped_code`), defaults 2/3;
  fleet-telemetry-v1 PR #1 → defaults 1/2, one `approve` with no findings.

Across every v2 run so far (clean 6 + posted 2 + partial 5): rounding reported on DriftZero PR #1 in **7/7** runs;
defaults reported in **6/7** on DriftZero PR #1 but only **2/6** on fleet-telemetry-v1 PR #1.

## On a fresh diff: fleet-telemetry-v1 PR #2

The live `driftzero migrate --review-fix` run (task `20261030-041743-ffdf04`) produced a new repair that **again
rounds the derived Euler angles** (`roll=round(eroll, 3)`, same for pitch and yaw), this time with the comment "wire
carries ~6-decimal quaternions, so round to millidegrees (well within v1's 0.1-degree precision)". The unchanged
`frame.pose.yaw == 87.0` test was in the reviewer's context. Reviewer v2 (`high`, one run) **approved with no
findings**, so `--review-fix` had nothing to act on. Its reasoning drafted the finding ("rounding is magic constant
to satisfy exact equality test, not required by spec"), half-accepted the new justification because the fixture
really carries 6-decimal quaternions, and then applied the prompt's own rule: "Given uncertainty, safest is to
approve with no findings, as per 'Report nothing you are unsure is real'." It also asked itself "Would an empty
findings list with approve be considered incorrect by evaluator?". Full trace:
[`runs/20261030-041743-ffdf04.jsonl`](runs/20261030-041743-ffdf04.jsonl) (`review` event).

So on the one unseen diff with the smell, v2 caught it **0/1**. The 3/3 above is on the diff whose miss motivated
v2's design; this run is the more honest signal of generalisation, and it is a miss.

## Reading

- The rounding is a reliable catch for v2 on DriftZero PR #1: every run that saw it reported it, varying only in
  label and severity. On a fresh diff with the same smell and a more plausible justification (PR #2) it was missed,
  so the 3/3 does not transfer.
- The defaults issue is reliable on one PR and mostly missed on the other. On fleet-telemetry-v1 the reviewer
  approved with no findings in 4 of 6 runs, so a single "approve" from it is weak evidence.
- Small numbers (3 runs per cell, one model, one effort level, two diffs). This measures run-to-run variance on
  known issues; it says nothing about false positives or issues nobody has labelled.
