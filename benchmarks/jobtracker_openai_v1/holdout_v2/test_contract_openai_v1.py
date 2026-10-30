"""Hold-out contract tests for JobTracker's OpenAI Python SDK v0 -> v1 migration.

Never shown to the repair agent. Derived from the official v1.0.0 migration guide
(github.com/openai/openai-python/discussions/742, quoted in CHANGELOG_openai_v1.md) and the v1 SDK's
public interface. The API is mocked at the HTTP layer with respx (the v1 SDK's transport is httpx),
so these tests do not depend on how the client is structured (module-level, per instance, or the
module client the guide still documents) -- only on the v1 calls being made and behaving as before.
"""
import ast
import json
import os
import pathlib

os.environ.setdefault("OPENAI_API_KEY", "sk-test-holdout")  # config.py reads it at import time
os.environ.pop("OPENAI_BASE_URL", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://api.openai.com/v1"

MODELS = {"object": "list", "data": [
    {"id": "gpt-4-1106-preview", "object": "model", "created": 0, "owned_by": "openai"},
    {"id": "whisper-1", "object": "model", "created": 0, "owned_by": "openai"},
    {"id": "gpt-3.5-turbo", "object": "model", "created": 0, "owned_by": "openai"},
]}


def completion(finish_reason, message):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4-1106-preview",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/models").mock(return_value=httpx.Response(200, json=MODELS))
        yield mock


def make_bot():
    from src.JobTracker.chatbot import ChatGPT

    return ChatGPT()


def test_no_v0_resource_calls_remain():
    # Guide, "All name changes": openai.ChatCompletion.create() -> client.chat.completions.create(),
    # openai.Model.list() -> client.models.list()
    for path in (ROOT / "src" / "JobTracker").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "openai":
                assert node.attr not in ("ChatCompletion", "Model"), f"{path.name}: openai.{node.attr} is v0 API"


def test_model_list_through_v1_client(api):
    bot = make_bot()
    assert api.calls.call_count >= 1 and api.calls.last.request.url.path == "/v1/models"
    assert bot.models == {"gpt-4-1106-preview", "gpt-3.5-turbo"}  # same "gpt" filter as before


def test_model_list_api_error_is_handled(api):
    api.get("/models").mock(return_value=httpx.Response(401, json={"error": {"message": "bad key", "type": "invalid_request_error"}}))
    bot = make_bot()  # v0 behaviour: the API error is caught and logged, construction succeeds
    assert bot.models == set()


def test_function_call_response_read_by_attribute(api):
    from src.JobTracker.config import FUNCTION, MODEL

    args = json.dumps({"company": "TestCompany", "state": "TestState", "next_step": "TestNextStep"})
    route = api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion(
        "function_call", {"role": "assistant", "content": None,
                          "function_call": {"name": "get_email_info", "arguments": args}})))
    bot = make_bot()
    info = {"body": "test email body", "date": "Thu, 09 Nov 2023 23:27:06 +0000"}
    state, data = bot.get_content(info)
    assert state == "Succeed", data
    assert (data["company"], data["next_step"]) == ("TestCompany", "TestNextStep")
    assert data["state"] == json.dumps({"TestState": "Nov 09 2023 23:27:06"})
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == MODEL
    assert sent["messages"] == [{"role": "user", "content": bot.gen_prompt("test email body")}]
    assert sent["functions"] == FUNCTION


def test_non_function_reply_is_not_a_job_email(api):
    api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion(
        "stop", {"role": "assistant", "content": "Not a job email."})))
    state, data = make_bot().get_content({"body": "newsletter", "date": "Thu, 09 Nov 2023 23:27:06 +0000"})
    assert (state, data) == ("Failed", "Not related to a job application or interview process")


def test_api_error_is_caught_with_v1_error_classes(api):
    api.post("/chat/completions").mock(return_value=httpx.Response(
        400, json={"error": {"message": "bad request", "type": "invalid_request_error"}}))
    state, data = make_bot().get_content({"body": "x", "date": "Thu, 09 Nov 2023 23:27:06 +0000"})
    assert state == "Failed" and data.startswith("OpenAI API error:"), data


def test_requirements_allow_openai_v1():
    from packaging.requirements import Requirement

    reqs = [Requirement(line.split("#")[0].strip()) for line in (ROOT / "requirements.txt").read_text().splitlines()
            if line.split("#")[0].strip() and not line.startswith("-")]
    openai_req = next(r for r in reqs if r.name.lower() == "openai")
    assert openai_req.specifier.contains("1.0.0") or any(openai_req.specifier.contains(f"1.{m}.0") for m in range(1, 120)), \
        f"requirements.txt pins {openai_req} (guide: `pip install --upgrade openai`)"
