<!--
Provenance (added by DriftZero, not part of the quoted text):
Source: https://github.com/openai/openai-python/discussions/742  ("v1.0.0 Migration Guide", posted 2023-11-08 by @rattrayalex in openai/openai-python).
Everything below this comment is quoted verbatim from that discussion body, fetched 2026-10-30 via the GitHub GraphQL API.
Sections omitted as not relevant to this repo: grit migration tooling (incl. Windows/WSL and Jupyter notes), Async client, Azure OpenAI.
No text has been added, reworded or corrected.
-->

We have released a new major version of our SDK, and we recommend upgrading promptly. 

It's a total rewrite of the library, so many things have changed, but we've made upgrading easy with a code migration script and detailed docs below. It was extensively [beta tested](https://github.com/openai/openai-python/discussions/631) prior to release.

## Getting started

```
pip install --upgrade openai
```

## What's changed
- Auto-retry with backoff if there's an error
- Proper types (for mypy/pyright/editors)
- You can now instantiate a client, instead of using a global default.
- Switch to explicit client instantiation
- Weights and Biases CLI will now be included in their own package

### Initialization

```py
# old
import openai

openai.api_key = os.environ['OPENAI_API_KEY']

# new
from openai import OpenAI

client = OpenAI(
  api_key=os.environ['OPENAI_API_KEY'],  # this is also the default, it can be omitted
)
```

### Responses

Response objects are now [pydantic models](https://docs.pydantic.dev/latest/usage/models/) and no longer conform to the dictionary shape. However you can easily convert them to a dictionary with `model.model_dump()`.

```py
# before
import json
import openai

completion = openai.Completion.create(model='curie')
print(completion['choices'][0]['text'])
print(completion.get('usage'))
print(json.dumps(completion, indent=2))

# after
from openai import OpenAI

client = OpenAI()

completion = client.completions.create(model='curie')
print(completion.choices[0].text)
print(dict(completion).get('usage'))
print(completion.model_dump_json(indent=2))
```

### Module client

> [!IMPORTANT]  
> We highly recommend instantiating client instances instead of relying on the global client.

We also expose a global client instance that is accessible in a similar fashion to versions prior to v1.

```py
import openai

# optional; defaults to `os.environ['OPENAI_API_KEY']`
openai.api_key = '...'

# all client options can be configured just like the `OpenAI` instantiation counterpart
openai.base_url = "https://..."
openai.default_headers = {"x-foo": "true"}

completion = openai.chat.completions.create(
    model="gpt-4",
    messages=[
        {
            "role": "user",
            "content": "How do I output all files in a directory using Python?",
        },
    ],
)
print(completion.choices[0].message.content)
```

The API is the exact same as the standard client instance based API.

This is intended to be used within REPLs or notebooks for faster iteration, **not** in application code.

We recommend that you always instantiate a client (e.g., with `client = OpenAI()`) in application code because:
- It can be difficult to reason about where client options are configured
- It's not possible to change certain client options without potentially causing race conditions
- It's harder to mock for testing purposes
- It's not possible to control cleanup of network connections

### Pagination

All `list()` methods that support pagination in the API now support automatic iteration, for example:

```py
from openai import OpenAI

client = OpenAI()

for job in client.fine_tuning.jobs.list(limit=1):
    print(job)
```

Previously you would have to explicitly call a `.auto_paging_iter()` method instead.
See the[ README](https://github.com/openai/openai-python/tree/main/README.md) for more details.

### All name changes

> Note: all `a*` methods have been removed; the [async client](#async-client) must be used instead.

- `openai.api_base` -> `openai.base_url`
- `openai.proxy` -> `openai.proxies` ([docs](https://www.python-httpx.org/compatibility/#proxy-keys))
- `openai.InvalidRequestError` -> `openai.BadRequestError`
- `openai.Audio.transcribe()` -> `client.audio.transcriptions.create()`
- `openai.Audio.translate()` -> `client.audio.translations.create()`
- `openai.ChatCompletion.create()` -> `client.chat.completions.create()`
- `openai.Completion.create()` -> `client.completions.create()`
- `openai.Edit.create()` -> `client.edits.create()`
- `openai.Embedding.create()` -> `client.embeddings.create()`
- `openai.File.create()` -> `client.files.create()`
- `openai.File.list()` -> `client.files.list()`
- `openai.File.retrieve()` -> `client.files.retrieve()`
- `openai.File.download()` -> `client.files.retrieve_content()`
- `openai.FineTune.cancel()` -> `client.fine_tunes.cancel()`
- `openai.FineTune.list()` -> `client.fine_tunes.list()`
- `openai.FineTune.list_events()` -> `client.fine_tunes.list_events()`
- `openai.FineTune.stream_events()` -> `client.fine_tunes.list_events(stream=True)`
- `openai.FineTune.retrieve()` -> `client.fine_tunes.retrieve()`
- `openai.FineTune.delete()` -> `client.fine_tunes.delete()`
- `openai.FineTune.create()` -> `client.fine_tunes.create()`
- `openai.FineTuningJob.create()` -> `client.fine_tuning.jobs.create()`
- `openai.FineTuningJob.cancel()` -> `client.fine_tuning.jobs.cancel()`
- `openai.FineTuningJob.delete()` -> `client.fine_tuning.jobs.create()`
- `openai.FineTuningJob.retrieve()` -> `client.fine_tuning.jobs.retrieve()`
- `openai.FineTuningJob.list()` -> `client.fine_tuning.jobs.list()`
- `openai.FineTuningJob.list_events()` -> `client.fine_tuning.jobs.list_events()`
- `openai.Image.create()` -> `client.images.generate()`
- `openai.Image.create_variation()` -> `client.images.create_variation()`
- `openai.Image.create_edit()` -> `client.images.edit()`
- `openai.Model.list()` -> `client.models.list()`
- `openai.Model.delete()` -> `client.models.delete()`
- `openai.Model.retrieve()` -> `client.models.retrieve()`
- `openai.Moderation.create()` -> `client.moderations.create()`
- `openai.api_resources` -> `openai.resources`

### Removed

- `openai.api_key_path`
- `openai.app_info`
- `openai.debug`
- `openai.log`
- `openai.OpenAIError`
- `openai.Audio.transcribe_raw()` 
- `openai.Audio.translate_raw()`
- `openai.ErrorObject`
- `openai.Customer`
- `openai.api_version`
- `openai.verify_ssl_certs`
- `openai.api_type`
- `openai.enable_telemetry`
- `openai.ca_bundle_path`
- `openai.requestssession` (we now use [httpx](https://github.com/encode/httpx))
- `openai.aiosession` (we now use [httpx](https://github.com/encode/httpx))
- `openai.Deployment` (only used for Azure) – please use the [azure-mgmt-cognitiveservices](https://pypi.org/project/azure-mgmt-cognitiveservices) client library instead (here's [how to list deployments](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cognitiveservices/azure-mgmt-cognitiveservices/generated_samples/list_deployments.py), for example).
- `openai.Engine`
- `openai.File.find_matching_files()`
- `openai.embeddings_utils` (now in the cookbook)

