"""Hold-out contract tests for wolverine's OpenAI Python SDK v0 -> v1 migration.

Never shown to the repair agent. Derived from the official v1.0.0 migration guide
(github.com/openai/openai-python/discussions/742, quoted in benchmarks/openai_v0_to_v1/CHANGELOG_openai_v1.md)
and the v1 SDK's public interface. The API is mocked at the HTTP layer with respx (the v1 SDK's transport is
httpx), so these tests do not depend on how the client is structured (module-level, lazily created, or the
module client the guide still documents) -- only on the v1 calls being made and wolverine behaving as before.
"""
import ast
import json
import os
import pathlib

os.environ["OPENAI_API_KEY"] = "sk-test-holdout"  # wolverine reads it at import time (load_dotenv does not override)
os.environ.pop("OPENAI_BASE_URL", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://api.openai.com/v1"
CHANGES = [{"explanation": "y is undefined"}, {"operation": "Replace", "line": 2, "content": "print(x)"}]
RETRY_MSG = "Your response could not be parsed by json.loads. Please restate your last message as pure JSON."


def completion(content):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-4",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def models(*ids):
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 0, "owned_by": "openai"} for i in ids]}


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


def sent(call):
    return json.loads(call.request.content)


def test_no_v0_resource_calls_remain():
    # Guide, "All name changes": openai.ChatCompletion.create() -> client.chat.completions.create(),
    # openai.Model.list() -> client.models.list()
    for path in (ROOT / "wolverine").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "openai":
                assert node.attr not in ("ChatCompletion", "Model"), f"{path.name}: openai.{node.attr} is v0 API"


def test_json_response_through_v1_chat_completions(api):
    from wolverine.wolverine import json_validated_response

    route = api.post("/chat/completions").mock(return_value=httpx.Response(
        200, json=completion("Here are the changes:\n" + json.dumps(CHANGES))))
    messages = [{"role": "user", "content": "prompt"}]
    assert json_validated_response("gpt-4", list(messages), nb_retry=1) == CHANGES
    assert route.call_count == 1
    body = sent(route.calls.last)
    assert (body["model"], body["temperature"], body["messages"]) == ("gpt-4", 0.5, messages)
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test-holdout"


def test_invalid_json_is_retried_with_conversation_history(api):
    from wolverine.wolverine import json_validated_response

    route = api.post("/chat/completions").mock(side_effect=[
        httpx.Response(200, json=completion("Sorry, no JSON here.")),
        httpx.Response(200, json=completion(json.dumps(CHANGES))),
    ])
    assert json_validated_response("gpt-4", [{"role": "user", "content": "prompt"}], nb_retry=3) == CHANGES
    assert route.call_count == 2
    second = sent(route.calls[1])["messages"]
    assert len(second) == 3 and second[0] == {"role": "user", "content": "prompt"}
    assert (second[1]["role"], second[1]["content"]) == ("assistant", "Sorry, no JSON here.")
    assert second[2] == {"role": "user", "content": RETRY_MSG}


def test_retries_exhausted_raises(api):
    from wolverine.wolverine import json_validated_response

    route = api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion("still not json")))
    with pytest.raises(Exception, match="No valid json response found"):
        json_validated_response("gpt-4", [{"role": "user", "content": "prompt"}], nb_retry=2)
    assert route.call_count == 2


def test_send_error_to_gpt_builds_the_same_prompt(api, tmp_path):
    from wolverine.wolverine import send_error_to_gpt

    script = tmp_path / "buggy.py"
    script.write_text("x = 1\nprint(y)\n")
    route = api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion(json.dumps(CHANGES))))
    assert send_error_to_gpt(str(script), ["a", 2], "NameError: name 'y' is not defined",
                             model="gpt-3.5-turbo") == CHANGES
    body = sent(route.calls.last)
    assert body["model"] == "gpt-3.5-turbo"
    system, user = body["messages"]
    assert system == {"role": "system", "content": (ROOT / "prompt.txt").read_text()}
    assert user["role"] == "user"
    for needle in ("1: x = 1\n2: print(y)\n", "['a', 2]", "NameError: name 'y' is not defined"):
        assert needle in user["content"], needle


def test_model_available_through_v1_models_list(api):
    from wolverine.wolverine import check_model_availability

    route = api.get("/models").mock(return_value=httpx.Response(200, json=models("gpt-3.5-turbo", "gpt-4")))
    assert check_model_availability("gpt-4") is None
    assert route.call_count == 1


def test_unavailable_model_exits(api, capsys):
    from wolverine.wolverine import check_model_availability

    api.get("/models").mock(return_value=httpx.Response(200, json=models("gpt-3.5-turbo", "whisper-1")))
    with pytest.raises(SystemExit):
        check_model_availability("gpt-4")
    assert "Model gpt-4 is not available" in capsys.readouterr().out
