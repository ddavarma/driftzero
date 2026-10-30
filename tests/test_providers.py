import json
import os
from types import SimpleNamespace

import pytest

from driftzero import providers


class FakeResponses:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            id="resp_123",
            model=kwargs["model"],
            status="completed",
            output_text=self.text,
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=150,
                output_tokens_details=SimpleNamespace(reasoning_tokens=140),
            ),
        )


def fake_client(text="OK"):
    return SimpleNamespace(responses=FakeResponses(text))


def test_respond_uses_responses_api_with_defaults(tmp_path):
    client = fake_client()
    log = tmp_path / "runs" / "r1.jsonl"
    r = providers.respond("hi", instructions="be terse", client=client, log_path=log)

    assert client.responses.kwargs == {
        "model": "muse-spark-1.3",
        "input": "hi",
        "max_output_tokens": 2000,
        "instructions": "be terse",
    }
    assert (r.text, r.input_tokens, r.output_tokens, r.reasoning_tokens) == ("OK", 12, 150, 140)

    record = json.loads(log.read_text().splitlines()[0])
    assert record["event"] == "llm_call"
    assert record["model"] == "muse-spark-1.3"
    assert "text" not in record


def test_empty_output_is_an_error():
    with pytest.raises(providers.ProviderError, match="empty output"):
        providers.respond("hi", client=fake_client(text=""))


def test_missing_key_is_reported_without_leaking(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    providers.get_client.cache_clear()
    with pytest.raises(providers.ProviderError, match="FIREWORKS_API_KEY is not set"):
        providers.get_client("fireworks")


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("META_API_KEY"), reason="META_API_KEY not set")
def test_live_meta_responses_call(tmp_path):
    providers.get_client.cache_clear()
    log = tmp_path / "live.jsonl"
    r = providers.respond("Reply with the single word OK.", log_path=log)
    assert "ok" in r.text.lower()
    assert r.output_tokens > 0
    assert log.exists()
    print(f"\nlive: {r.model} text={r.text!r} in={r.input_tokens} out={r.output_tokens} "
          f"reasoning={r.reasoning_tokens} latency={r.latency_s}s status={r.status}")
