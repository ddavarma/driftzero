import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from driftzero import differ
from driftzero.spec import DriftSpec

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "robotics_telemetry_drift"
V1, V2, CHANGELOG = BENCH / "openapi_v1.json", BENCH / "openapi_v2.json", BENCH / "CHANGELOG_v2.md"
HAND = DriftSpec.load(BENCH / "driftspec_v2.json")


class FakeClient:
    def __init__(self, transform):
        self.transform = transform
        self.kwargs = None
        self.responses = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        text = self.transform(kwargs)
        usage = SimpleNamespace(input_tokens=5000, output_tokens=3000,
                                input_tokens_details=SimpleNamespace(cached_tokens=1000),
                                output_tokens_details=SimpleNamespace(reasoning_tokens=1000))
        return SimpleNamespace(id="r", model="muse-spark-1.3", status="completed", output_text=text, usage=usage)


def skeleton_from_prompt(kwargs) -> dict:
    """The skeleton JSON embedded in the prompt (second ```json block)."""
    blocks = kwargs["input"].split("```json\n")
    return json.loads(blocks[2].split("\n```")[0])


# ---------------------------------------------------------------- pass 1


def keys(spec, cat):
    return {differ._key(cat, e) for e in getattr(spec, cat)}


def test_prepass_on_benchmark_matches_hand_written_structure():
    v1, v2 = json.loads(V1.read_text()), json.loads(V2.read_text())
    skel = differ.skeleton(differ.openapi_candidates(v1, v2), v1, v2)
    assert (skel.api, skel.from_version, skel.to_version) == ("Fleet Telemetry API", "v1", "v2")
    for cat in differ.CATEGORIES:
        assert keys(skel, cat) == keys(HAND, cat), cat
    kinds = {c.kind for c in differ.openapi_candidates(v1, v2)}
    assert {"schema_removed", "schema_added"} <= kinds  # EulerOrientation -> Quaternion, as context


def doc(prefix, paths, schemas, example=None):
    d = {"openapi": "3.1.0", "info": {"title": "T", "version": "1.0.0"}, "servers": [{"url": f"https://x.io{prefix}"}],
         "paths": {}, "components": {"schemas": schemas}}
    for p in paths:
        resp = {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}
        if example is not None:
            resp["content"]["application/json"]["example"] = example
        d["paths"][p] = {"get": {"operationId": p, "responses": {"200": resp}}}
    return d


def test_prepass_synthetic_changes():
    item1 = {"type": "object", "required": ["id", "kind"], "properties": {
        "id": {"type": "string"}, "kind": {"type": "string", "enum": ["a", "b"]},
        "price": {"type": "number"}, "note": {"type": "string"}}}
    item2 = {"type": "object", "required": ["id", "kind", "note"], "properties": {
        "id": {"type": "string"}, "kind": {"type": "string", "enum": ["a", "b", "c"]},
        "price": {"type": "integer"}, "note": {"type": "string"}, "currency": {"type": "string"}}}
    v1 = doc("/api/v1", ["/items/{id}", "/legacy"], {"Item": item1}, example={"id": "x", "price": 1.5})
    v2 = doc("/api/v2", ["/items/{id}", "/new"], {"Item": item2}, example={"id": "y", "price": 150})
    got = {(c.kind, c.schema_name, c.path, json.dumps(c.old), json.dumps(c.new)) for c in differ.openapi_candidates(v1, v2)}
    assert ("endpoint_renamed", "", "/api/v1/items/{id}", '"GET /api/v1/items/{id}"', '"GET /api/v2/items/{id}"') in got
    assert ("endpoint_removed", "", "/api/v1/legacy", '"GET /api/v1/legacy"', "null") in got
    assert ("endpoint_added", "", "/api/v2/new", "null", '"GET /api/v2/new"') in got
    assert ("field_added", "Item", "currency", "null", '"string"') in got
    assert ("type_changed", "Item", "price", '"number"', '"integer"') in got
    assert ("required_changed", "Item", "note", "false", "true") in got
    assert ("value_changed", "Item", "kind", '["a", "b"]', '["a", "b", "c"]') in got
    assert ("example_changed", "Item", "id", '"x"', '"y"') in got
    assert ("example_changed", "Item", "price", "1.5", "150") in got


def test_strict_schema_requires_everything():
    schema = differ.strict_schema(DriftSpec)
    objects = [schema, *schema["$defs"].values()]
    for o in objects:
        assert o["additionalProperties"] is False and set(o["required"]) == set(o["properties"])
    assert "default" not in json.dumps(schema)


# ---------------------------------------------------------------- pass 2: merge


def test_merge_keeps_structure_takes_annotations_and_additions():
    def model(kwargs):
        s = skeleton_from_prompt(kwargs)
        s["summary"] = "Euler to quaternion, noise to covariance."
        noise = next(f for f in s["fields_removed"] if f["path"] == "noise")
        noise["replacement"] = "covariance"
        noise["notes"] = "sigma = sqrt(trace(P_pos)/3)"
        noise["old_type"] = "string"            # structure edit: must be ignored
        s["fields_removed"] = [f for f in s["fields_removed"] if f["path"] != "orientation.pitch"]  # dropped: restored
        s["fields_renamed"].append({"schema_name": "TelemetryFrame", "old_path": "ts", "new_path": "timestamp",
                                    "notes": "from changelog"})
        return json.dumps(s)

    client = FakeClient(model)
    res = differ.diff(CHANGELOG, V1, V2, client=client)
    fmt = client.kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "DriftSpec"
    assert "CHANGELOG" in client.kwargs["input"] and "atan2" in client.kwargs["input"]
    assert client.kwargs["max_output_tokens"] >= 2000

    noise = next(f for f in res.spec.fields_removed if f.path == "noise")
    assert (noise.replacement, noise.notes, noise.old_type) == ("covariance", "sigma = sqrt(trace(P_pos)/3)", "number")
    assert keys(res.spec, "fields_removed") == keys(HAND, "fields_removed")  # pitch restored
    assert res.restored == ["fields_removed:TelemetryFrame.orientation.pitch"]
    assert res.model_added == ["fields_renamed:TelemetryFrame.ts"]
    assert res.spec.summary.startswith("Euler to quaternion")
    # 4000 uncached * 1.25 + 1000 cached * 0.15 + 3000 out * 4.25, per M
    assert res.cost.usd == pytest.approx((4000 * 1.25 + 1000 * 0.15 + 3000 * 4.25) / 1e6)


def test_changelog_only_takes_model_structure():
    spec = HAND.model_copy(deep=True)
    res = differ.diff(CHANGELOG, client=FakeClient(lambda kw: spec.model_dump_json()))
    assert res.candidates == [] and res.restored == []
    assert keys(res.spec, "fields_removed") == keys(HAND, "fields_removed")
    assert len(res.model_added) == sum(len(getattr(HAND, c)) for c in differ.CATEGORIES)
    assert res.spec.api == "Fleet Telemetry API"


def test_needs_some_input():
    with pytest.raises(ValueError):
        differ.diff(openapi_v1=V1)


def test_compare_reports_set_differences():
    gen = HAND.model_copy(deep=True)
    gen.fields_removed = [f for f in gen.fields_removed if f.path != "noise"]
    gen.fields_removed[0].replacement = "quaternion"
    gen.value_changes = []
    report = differ.compare(gen, HAND)
    assert report["fields_removed"]["only_reference"] == [["TelemetryFrame", "noise"]]
    assert report["fields_removed"]["replacement_differs"][0][0] == ["TelemetryFrame", "orientation.roll"]
    assert report["value_changes"]["only_reference"] == [["FleetSnapshot", "api_version"]]
    text = differ.format_comparison(report)
    assert "endpoints_renamed: match" in text and "only in hand-written: TelemetryFrame.noise" in text


@pytest.mark.live
@pytest.mark.skipif(os.getenv("DRIFTZERO_LIVE") != "1", reason="billed call; set DRIFTZERO_LIVE=1 to run")
def test_live_diff_vs_hand_written():
    res = differ.diff(CHANGELOG, V1, V2)
    report = differ.compare(res.spec, HAND)
    print("\n" + differ.format_comparison(report))
    print(f"model added: {res.model_added}\nrestored: {res.restored}\ncost: ${res.cost.usd:.4f}, "
          f"{res.cost.input_tokens} in / {res.cost.output_tokens} out, {res.latency_s}s")
    # the deterministic pass alone fixes these; the model can only add to them
    for cat in ("endpoints_renamed", "fields_removed", "fields_added", "type_changes", "value_changes"):
        assert not report[cat]["only_reference"], (cat, report[cat])
