import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from driftzero import reviewer
from driftzero.spec import DriftSpec

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "robotics_telemetry_drift"
SPEC = DriftSpec.load(BENCH / "driftspec_v2.json")

DIFF = """diff --git a/fleet_telemetry/client.py b/fleet_telemetry/client.py
index 1..2 100644
--- a/fleet_telemetry/client.py
+++ b/fleet_telemetry/client.py
@@ -40,6 +40,7 @@ def parse_frame(payload):
     try:
         pos = payload["position"]
-        roll = float(ori["roll"])
+        qx = float(ori["qx"])
+        roll, pitch, yaw = (round(v, 3) for v in to_euler(qx, qy, qz, qw))
         pose = Pose(
diff --git a/tests/fixtures/new.json b/tests/fixtures/new.json
new file mode 100644
--- /dev/null
+++ b/tests/fixtures/new.json
@@ -0,0 +1,2 @@
+{"a": 1}
+{"b": 2}
"""

GOOD = {"verdict": "comment", "findings": [{
    "severity": "medium", "category": "precision_loss", "file": "fleet_telemetry/client.py", "line_hint": "43",
    "evidence": "+        roll, pitch, yaw = (round(v, 3) for v in to_euler(qx, qy, qz, qw))",
    "explanation": "e", "suggested_fix": "f"}]}


class FakeChat:
    def __init__(self, replies, finish="stop"):
        self.finish = finish
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append(kw)
        content = self.replies.pop(0)
        usage = SimpleNamespace(prompt_tokens=10000, completion_tokens=4000,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=2000),
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=3900))
        msg = SimpleNamespace(content=content, reasoning_content="thinking about " + str(len(self.calls)))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=self.finish)], usage=usage)


def test_review_parses_json_mode_and_logs_reasoning(tmp_path):
    client = FakeChat([json.dumps(GOOD)])
    log = tmp_path / "run.jsonl"
    out = reviewer.review(DIFF, ["[stale_test_name] tests/x.py::test_v1"], SPEC, "changelog text", client=client,
                          log_path=log, task_id="t")
    kw = client.calls[0]
    assert kw["model"] == "meta-models/muse-spark-open-1026" and kw["max_tokens"] == 24000
    assert kw["response_format"] == {"type": "json_object"} and kw["extra_body"] == {"reasoning_effort": "high"}
    system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
    assert "ALREADY checks the test files" in system and '"verdict"' in system
    assert "[stale_test_name] tests/x.py::test_v1" in user and "changelog text" in user and "```diff" in user
    assert out.result.findings[0].category == "precision_loss" and out.attempts == 1
    # (8000 uncached * 1.25 + 2000 cached * 0.15 + 4000 out * 4.25) / 1M
    assert out.cost.usd == pytest.approx((8000 * 1.25 + 2000 * 0.15 + 4000 * 4.25) / 1e6)
    rec = json.loads(log.read_text())
    assert rec["event"] == "review" and rec["reasoning_content"] == ["thinking about 1"] and rec["cost"]["usd"]


def test_one_retry_on_parse_failure_then_give_up():
    ok = reviewer.review(DIFF, [], SPEC, None, client=FakeChat(["not json", json.dumps(GOOD)]))
    assert ok.attempts == 2 and ok.result is not None and ok.error is None
    bad = reviewer.review(DIFF, [], SPEC, None, client=FakeChat(["", '{"verdict": "maybe", "findings": []}']))
    assert bad.result is None and bad.attempts == 2 and "attempt 2" in bad.error


def test_fireworks_provider_switches_model_only():
    client = FakeChat([json.dumps({"verdict": "approve", "findings": []})])
    out = reviewer.review(DIFF, [], SPEC, None, provider="fireworks", client=client)
    assert client.calls[0]["model"] == "accounts/fireworks/models/muse-spark-open"
    assert out.result.verdict == "approve" and out.cost.usd is not None


def test_prompt_does_not_mention_rounding():
    # the demo claim depends on the reviewer finding test-shaped code unaided
    assert "round" not in reviewer.SYSTEM.lower()


def test_diff_line_mapping():
    lines = reviewer.diff_new_lines(DIFF)
    assert lines["fleet_telemetry/client.py"][43].strip().startswith("roll, pitch, yaw = (round")
    assert 1 in lines["tests/fixtures/new.json"]
    f = reviewer.Finding(**GOOD["findings"][0])
    assert reviewer.locate(f, lines) == ("fleet_telemetry/client.py", 43)
    by_evidence = f.model_copy(update={"line_hint": "", "file": "client.py"})
    assert reviewer.locate(by_evidence, lines) == ("fleet_telemetry/client.py", 43)
    nowhere = f.model_copy(update={"line_hint": "999", "evidence": "+ something not in the diff"})
    assert reviewer.locate(nowhere, lines) is None
    # a wrong line_hint pointing at a context line loses to the quoted evidence (seen live: hint 38 = `try:`)
    wrong_hint = f.model_copy(update={"line_hint": "40"})
    assert 40 in lines["fleet_telemetry/client.py"]
    assert reviewer.locate(wrong_hint, lines) == ("fleet_telemetry/client.py", 43)
    hint_only = f.model_copy(update={"line_hint": "40", "evidence": "no plus-prefixed lines"})
    assert reviewer.locate(hint_only, lines) == ("fleet_telemetry/client.py", 40)


def test_post_review_inline_body_and_strict(monkeypatch):
    sent = []

    def fake_run(cmd, input=None, **kw):
        payload = json.loads(input)
        sent.append(payload)
        if payload["event"] == "REQUEST_CHANGES":
            return SimpleNamespace(returncode=1, stdout='{"message":"Can not request changes on your own pull request"}',
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout='{"html_url": "https://x/review"}', stderr="")

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    high = {"verdict": "request_changes", "findings": [
        {**GOOD["findings"][0], "severity": "high"},
        {**GOOD["findings"][0], "file": "elsewhere.py", "line_hint": "", "evidence": "x"}]}
    out = reviewer.ReviewOutcome(result=reviewer.ReviewResult.model_validate(high), provider="baseten",
                                 model="meta-models/muse-spark-open-1026", effort="high",
                                 cost=reviewer.Cost(1, 0, 1, 0, 0.01), latency_s=3, attempts=1)
    res = reviewer.post_review("o/r", 7, out, DIFF)
    assert res == {"event": "COMMENT", "url": "https://x/review", "inline": 1, "body_only": 1}
    assert sent[0]["comments"][0]["path"] == "fleet_telemetry/client.py" and sent[0]["comments"][0]["line"] == 43
    assert "elsewhere.py" in sent[0]["body"]
    sent.clear()
    res = reviewer.post_review("o/r", 7, out, DIFF, strict=True)
    assert [p["event"] for p in sent] == ["REQUEST_CHANGES", "COMMENT"] and res["event"] == "COMMENT"
    assert "Would request changes" in sent[1]["body"]


def test_length_is_incomplete_not_approve(tmp_path):
    client = FakeChat([""], finish="length")
    log = tmp_path / "r.jsonl"
    out = reviewer.review(DIFF, [], SPEC, None, client=client, log_path=log)
    assert out.status == "incomplete" and out.verdict_label == "reviewer_incomplete" and out.attempts == 1
    rec = json.loads(log.read_text())
    assert rec["status"] == "incomplete" and rec["verdict"] == "reviewer_incomplete" and rec["max_tokens"] == 24000
    # even a parseable "approve" cut off by the budget is not an approval
    cut = reviewer.review(DIFF, [], SPEC, None, client=FakeChat(['{"verdict": "approve", "findings": []}'], finish="length"))
    assert cut.verdict_label == "reviewer_incomplete"
    assert reviewer.outcome_from_record(rec).status == "incomplete"


def test_epistemics_rule_and_test_context_section():
    assert "CLAIMS, not\nfacts" in reviewer.SYSTEM or "CLAIMS, not facts" in reviewer.SYSTEM.replace("\n", " ")
    assert "unsupported justification is itself a finding" in reviewer.SYSTEM.replace("\n", " ")
    user = reviewer.build_user_prompt(DIFF, [], SPEC, None, "def test_x():\n    assert f() == 1.0")
    assert "## Unchanged tests exercising modified code" in user and "def test_x()" in user
    assert user.index("## Diff under review") < user.index("## Unchanged tests")


def test_covering_tests_on_real_repair():
    from driftzero.blast_radius import analyze

    diff = (Path(__file__).resolve().parents[1] / "docs" / "runs" / "20261030-014828-02a484.diff").read_text()
    br = analyze(BENCH, SPEC)
    text, names = reviewer.covering_tests(br, diff, "benchmarks/robotics_telemetry_drift",
                                          lambda rel: (BENCH / rel).read_text())
    # unchanged tests only: the ones Muse edited are already in the diff
    assert "tests/test_client.py::test_client_hits_v1_paths" not in names
    assert "tests/test_alerts.py::test_fleet_summary" not in names
    assert "tests/charging" not in " ".join(names)
    # exact numeric equality first
    assert names[0] == "tests/test_client.py::test_parse_frame_reads_pose_and_noise"
    assert "assert frame.pose.yaw == 87.0" in text
    assert text.count("\n") + 1 <= reviewer.TEST_CONTEXT_MAX_LINES
    small, few = reviewer.covering_tests(br, diff, "benchmarks/robotics_telemetry_drift",
                                         lambda rel: (BENCH / rel).read_text(), cap=12)
    assert 0 < len(few) < len(names) and small.count("\n") + 1 <= 12


def test_v21_prompt_requires_confidence_and_forbids_suppression():
    sys_ = reviewer.SYSTEM.replace("\n", " ")
    assert "Report nothing you are unsure is real" not in sys_
    assert "Suppression is not allowed; low confidence is." in sys_
    assert '"confidence": 0.0-1.0' in reviewer.SYSTEM
    assert "round" not in reviewer.SYSTEM.lower()


def test_v22_policy_section_and_no_rounding_in_policy():
    import re

    from driftzero.repair import hard_rules

    policy = hard_rules(SPEC, "benchmarks/robotics_telemetry_drift")
    user = reviewer.build_user_prompt(DIFF, [], SPEC, None, None, policy)
    assert "## Agreed migration policy (the repair agent's hard rules)" in user
    assert policy[1].startswith("2. Test changes are limited") and policy[1] in user
    assert "Do not report code that\npolicy requires" in reviewer.SYSTEM or "policy requires" in reviewer.SYSTEM
    assert not re.search(r"\bround", " ".join(policy).lower())  # 'around' is fine; 'round'/'rounding' is not
    assert "round" not in reviewer.SYSTEM.lower()


def test_v23_policy_scope_sentence_and_frozen_version():
    flat = " ".join(reviewer.SYSTEM.split())
    assert ("This policy governs which interfaces exist, not how values are computed; numeric fidelity to the "
            "DriftSpec and changelog formulas is always in scope.") in flat
    assert "round" not in reviewer.SYSTEM.lower()
    assert reviewer.ReviewOutcome(result=None, provider="baseten", model="m", effort="high", cost=reviewer.Cost(),
                                  latency_s=0, attempts=0).version == "2.3"
