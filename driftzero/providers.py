"""Single gateway for every LLM call DriftZero makes.

One OpenAI-compatible client per provider, with timeouts and retries. Meta calls
use the Responses API (Meta's recommendation); each call returns token usage and
latency, and is optionally appended to a run log (.driftzero/runs/<id>.jsonl).

Smoke test:  python -m driftzero.providers
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

DEFAULT_META_MODEL = "muse-spark-1.3"
DEFAULT_MAX_OUTPUT_TOKENS = 2000  # Muse models reason first; less headroom -> empty output
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 3


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_env: str


PROVIDERS: dict[str, Provider] = {
    "meta": Provider("meta", "https://api.meta.ai/v1", "META_API_KEY"),
    "baseten": Provider("baseten", "https://inference.baseten.co/v1", "BASETEN_API_KEY"),
    "fireworks": Provider("fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
}


class ProviderError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    latency_s: float
    response_id: str | None = None
    status: str | None = None


@lru_cache(maxsize=None)
def get_client(provider: str = "meta") -> OpenAI:
    try:
        p = PROVIDERS[provider]
    except KeyError:
        raise ProviderError(f"unknown provider {provider!r}; known: {sorted(PROVIDERS)}") from None
    key = os.getenv(p.key_env)
    if not key:
        raise ProviderError(f"{p.key_env} is not set (expected in {REPO_ROOT / '.env'})")
    return OpenAI(base_url=p.base_url, api_key=key, timeout=DEFAULT_TIMEOUT_S, max_retries=DEFAULT_MAX_RETRIES)


def respond(
    prompt: str,
    *,
    instructions: str | None = None,
    model: str = DEFAULT_META_MODEL,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    log_path: Path | None = None,
    client: OpenAI | None = None,
) -> LLMResult:
    """One Meta Model API call via the Responses API; returns `output_text` plus usage."""
    client = client or get_client("meta")
    kwargs = {"model": model, "input": prompt, "max_output_tokens": max_output_tokens}
    if instructions:
        kwargs["instructions"] = instructions

    start = time.perf_counter()
    resp = client.responses.create(**kwargs)
    latency = time.perf_counter() - start

    usage = resp.usage
    details = getattr(usage, "output_tokens_details", None) if usage else None
    result = LLMResult(
        text=resp.output_text or "",
        provider="meta",
        model=getattr(resp, "model", None) or model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
        latency_s=round(latency, 3),
        response_id=getattr(resp, "id", None),
        status=getattr(resp, "status", None),
    )
    if log_path is not None:
        log_call(log_path, result)
    if not result.text.strip():
        raise ProviderError(
            f"{model} returned empty output (status={result.status}, "
            f"output_tokens={result.output_tokens}/{max_output_tokens}); raise max_output_tokens"
        )
    return result


def log_call(log_path: Path, result: LLMResult) -> None:
    """Append one call record (no prompt/response text) to a run's JSONL log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "event": "llm_call", **asdict(result)}
    record.pop("text")
    with log_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    r = respond("Reply with the single word OK.")
    print(f"{r.provider}/{r.model}: {r.text!r}  in={r.input_tokens} out={r.output_tokens} "
          f"(reasoning {r.reasoning_tokens})  {r.latency_s}s")
