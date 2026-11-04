# DriftZero demo video script (3:00), final

Numbers are from run `20261104-125710-21bcab` (fleet-telemetry-v1 PR #6), `docs/reviewer_eval.md`, and the
JobTracker and wolverine runs. Speak each line to one engineer; do not read.

Footage plan: the live run is 502 s; speed it up everywhere except three held moments:
the red `hold-outs 7/8 FAIL`, the green `8/8`, and the PR line.

---

## 0:00–0:12  Hook   [screen: watcher frame, iteration 1, `hold-outs 7/8`, red FAIL line; hold 2 s silent]

This coding agent just reported that every test passes. Of course they did. It wrote them. The hidden test it
never saw said otherwise. DriftZero is the referee the agent doesn't get to see.

## 0:12–0:25  The problem   [screen: a v1→v2 changelog next to a test file that mocks v1]

An upstream API changes. Your code still speaks v1, and so do your tests, because they mock the old response.
An agent that migrates honestly breaks the tests; an agent that makes them pass may have weakened them or
shaped the code around them. So "tests pass" from the agent proves nothing.

## 0:25–1:35  One live run   [screen: terminal with the watcher, sped up]

`driftzero migrate` on fleet-telemetry-v1, from the changelog and the two OpenAPI specs.

The differ: a deterministic diff of the specs finds sixteen candidates and keeps fourteen changes. Two endpoints
renamed, Euler angles replaced by quaternions, scalar noise replaced by a six-by-six covariance, a required
health field added. One call to Muse Spark 1.3 adds the migration formulas from the changelog. Two cents,
thirty-eight seconds.

Blast radius: Python's own parser, no model. Forty symbols in eleven files, sixteen tests.

Muse Code repairs it in a git worktree. Sixteen steps, thirty-six tool calls. Main is never touched.

The guard diffs the tests: zero rejections, sixteen changes listed for a human.

Then a fresh machine on Modal runs two suites. The agent's own tests: twenty-five of twenty-five. The eight
hold-out contract tests it has never seen: seven of eight. The health-range check failed.   [hold the red line]

Only the test name and the one-line reason go back. Iteration two: twelve steps, forty-nine seconds.
Twenty-five of twenty-five, and eight of eight.   [hold the green line]

A pull request on the target repo. Two iterations, sixty-five cents, about eight minutes end to end.   [hold]

## 1:35–2:15  The evidence   [screen: PR #6, slow scroll]

Everything is in the PR. What changed. The test edits a human should look at. The hold-out names and results.
The sandbox id. Tokens and cost. Links to the raw event logs.

Then a second opinion from a different model family. Muse Spark Open, Meta's open-weights model on Baseten,
reads the diff plus the unchanged tests that cover the changed code. On this PR it found an unreachable except
clause the tests never hit, confidence 0.88. Low severity, real. On our first benchmark PR it found three
defects that the tests, the guard and the hold-outs had all missed. It also misses things, and the repo says
how often.

## 2:15–2:45  Repos we didn't write   [screen: JobTracker PR #1, then wolverine PR #1, 8 s each]

Then we pointed it at two public repos we had never seen, stuck on the OpenAI Python SDK v0, with the official
v1 migration guide as the only input. Both verified. JobTracker: 173 seconds, forty cents, five of five and seven
of seven. Wolverine: 206 seconds, thirty-nine cents, six of six and seven of seven. The first attempt on each
failed, and those runs are in the README too, including the time the agent stubbed out the SDK so its tests would
import without it. The guard rejects that now.

## 2:45–3:00  Limits and link   [screen: README limitations, then the repo URL]

Python only. Hold-outs are written per target. The reviewer is a second opinion, not a gate. A wrong hold-out
stalls the run, and a human reads the log. Three Meta models, one loop, and a pull request you can check.
github.com/ddavarma/driftzero.

---

## Submission paragraph (under 120 words)

DriftZero migrates a codebase across a breaking API change and proves the migration before opening a pull
request. A deterministic spec diff plus one Muse Spark 1.3 call produce a change spec; Muse Code repairs the
code in an isolated worktree; a static guard rejects test tampering; a Modal sandbox runs the agent's tests and
hold-out contract tests the agent never saw; Muse Spark Open, Meta's open-weights model, reviews the diff for
what static analysis can't catch; then it opens a PR with every piece of evidence attached. Verified on our
benchmark and on two public repos we did not write, for under a dollar per migration. Built solo, Oct 26–31.

## Before you submit

- Video unlisted on YouTube, link in README.
- Repo public, `.env` absent, `docs/runs/` has run 20261104-125710-21bcab so PR #6's log links resolve.
- Form: repo link, video link, paragraph above, Indie track, you as the only participant.
- Submit by 6:00 PM ET. Deadline is 9:00 PM.
