"""Changelog / OpenAPI v1 -> v2  ->  DriftSpec.

Two passes:
1. Deterministic pre-pass (no LLM): JSON-diff the two OpenAPI documents into candidate
   changes: endpoints added/removed/renamed (renames matched by path suffix after the
   version segment), schema fields added/removed, type changes, required changes,
   enum/const changes, example value changes.
2. One muse-spark-1.3 call (Responses API, DriftSpec JSON schema as the structured output
   format) that merges the candidates with the changelog text: fills `replacement`
   mappings, migration formulas (notes), value changes, and anything the changelog states
   that the spec diff cannot see.

The model annotates; it does not invent structure. Every deterministic entry is kept with
its keys and types exactly as diffed; from the model's version of an entry only the
annotation fields (replacement, notes) are taken. Entries the model adds are kept and
reported as `model_added`; deterministic entries it dropped are restored and reported.

Smoke test:  python -m driftzero.differ --changelog C.md --openapi-v1 a.json --openapi-v2 b.json
"""
from __future__ import annotations

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from driftzero import providers
from driftzero.spec import (DriftSpec, EndpointRename, FieldAdded, FieldRemoved, FieldRenamed, TypeChange,
                            ValueChange)
from driftzero.telemetry import Cost

DIFF_MODEL = "muse-spark-1.3"
DIFF_MAX_OUTPUT_TOKENS = 16000  # the DriftSpec is a few K tokens and the model reasons first
VERSION_SEG = re.compile(r"^v\d+(?:\.\d+)*$")
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")


# --------------------------------------------------------------------------- pass 1: deterministic

@dataclass
class Candidate:
    kind: str  # endpoint_renamed | endpoint_removed | endpoint_added | field_removed | field_added | type_changed
    #            | required_changed | value_changed | example_changed | schema_removed | schema_added
    schema_name: str = ""
    path: str = ""
    old: Any = None
    new: Any = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v not in (None, "")}


def _server_prefix(doc: dict) -> str:
    url = ((doc.get("servers") or [{}])[0] or {}).get("url", "")
    m = re.match(r"^[a-z]+://[^/]+(/.*)?$", url)
    path = (m.group(1) if m else url) or ""
    return path.rstrip("/")


def _endpoints(doc: dict) -> dict[tuple[str, str], str]:
    """(METHOD, full path incl. server prefix) -> operationId."""
    prefix = _server_prefix(doc)
    out = {}
    for path, item in (doc.get("paths") or {}).items():
        for method in HTTP_METHODS:
            if method in (item or {}):
                out[(method.upper(), prefix + path)] = item[method].get("operationId", "")
    return out


def _unversioned(path: str) -> str:
    return "/" + "/".join(seg for seg in path.strip("/").split("/") if not VERSION_SEG.match(seg))


def _resolve(doc: dict, node: dict) -> tuple[dict, str | None]:
    ref = node.get("$ref")
    if not ref:
        return node, None
    name = ref.rsplit("/", 1)[-1]
    return (doc.get("components", {}).get("schemas", {}).get(name) or {}), name


def _type_label(doc: dict, node: dict) -> str:
    node_r, ref = _resolve(doc, node)
    if ref:
        return ref
    t = node_r.get("type")
    if t == "array":
        return f"array[{_type_label(doc, node_r.get('items') or {})}]"
    if isinstance(t, list):
        return "|".join(t)
    return t or ("object" if "properties" in node_r else "any")


def _flatten(doc: dict, schema: dict, prefix: str = "", seen: tuple = ()) -> dict[str, dict]:
    """dotted field path -> {type, required, const, enum, example} with object $refs inlined
    (array item $refs are not expanded, to avoid duplicating whole sub-schemas)."""
    out: dict[str, dict] = {}
    node, _ = _resolve(doc, schema)
    required = set(node.get("required") or [])
    for name, prop in (node.get("properties") or {}).items():
        path = f"{prefix}{name}"
        resolved, ref = _resolve(doc, prop)
        out[path] = {"type": _type_label(doc, prop), "required": name in required,
                     "const": resolved.get("const"), "enum": resolved.get("enum"), "example": resolved.get("example")}
        if (resolved.get("properties") and resolved.get("type", "object") == "object" and ref not in seen):
            out.update(_flatten(doc, prop, f"{path}.", seen + ((ref,) if ref else ())))
    return out


def _response_examples(doc: dict) -> dict[str, dict]:
    """schema name -> example payload, from 2xx JSON responses that $ref a schema."""
    out: dict[str, dict] = {}
    for item in (doc.get("paths") or {}).values():
        for method in HTTP_METHODS:
            for code, resp in ((item or {}).get(method, {}).get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                media = (resp.get("content") or {}).get("application/json") or {}
                ref = (media.get("schema") or {}).get("$ref")
                if ref and isinstance(media.get("example"), dict):
                    out.setdefault(ref.rsplit("/", 1)[-1], media["example"])
    return out


def _referenced_inline(doc: dict) -> set[str]:
    """Schemas only ever used as an object property of another schema (they are covered inline)."""
    schemas = doc.get("components", {}).get("schemas", {})
    from_props: set[str] = set()
    from_elsewhere: set[str] = set()

    def walk(node: Any, in_prop: bool) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                (from_props if in_prop else from_elsewhere).add(node["$ref"].rsplit("/", 1)[-1])
            for k, v in node.items():
                walk(v, in_prop=(k == "properties") or (in_prop and k not in ("items",)))
        elif isinstance(node, list):
            for v in node:
                walk(v, in_prop)

    for s in schemas.values():
        walk(s, False)
    walk(doc.get("paths") or {}, False)
    return from_props - from_elsewhere


def openapi_candidates(v1: dict, v2: dict) -> list[Candidate]:
    out: list[Candidate] = []

    # endpoints
    e1, e2 = _endpoints(v1), _endpoints(v2)
    removed = sorted(set(e1) - set(e2))
    added = sorted(set(e2) - set(e1))
    for m, p in removed:
        match = [(m2, p2) for (m2, p2) in added if m2 == m and _unversioned(p2) == _unversioned(p)]
        if len(match) == 1:
            out.append(Candidate("endpoint_renamed", path=p, old=f"{m} {p}", new=f"{m} {match[0][1]}",
                                 detail="matched by path suffix"))
            added.remove(match[0])
        else:
            out.append(Candidate("endpoint_removed", path=p, old=f"{m} {p}"))
    out += [Candidate("endpoint_added", path=p, new=f"{m} {p}") for m, p in added]

    # schemas
    s1 = v1.get("components", {}).get("schemas", {})
    s2 = v2.get("components", {}).get("schemas", {})
    inline1, inline2 = _referenced_inline(v1), _referenced_inline(v2)
    for name in sorted(set(s1) - set(s2)):
        out.append(Candidate("schema_removed", schema_name=name, detail="only in v1" + (" (inlined)" if name in inline1 else "")))
    for name in sorted(set(s2) - set(s1)):
        out.append(Candidate("schema_added", schema_name=name, detail="only in v2" + (" (inlined)" if name in inline2 else "")))
    ex1, ex2 = _response_examples(v1), _response_examples(v2)
    for name in sorted(set(s1) & set(s2)):
        if name in inline1 and name in inline2:
            continue  # reported inline under the schemas that embed it
        f1, f2 = _flatten(v1, s1[name]), _flatten(v2, s2[name])
        for path in sorted(set(f1) - set(f2)):
            out.append(Candidate("field_removed", name, path, old=f1[path]["type"]))
        for path in sorted(set(f2) - set(f1)):
            out.append(Candidate("field_added", name, path, new=f2[path]["type"],
                                 detail="required" if f2[path]["required"] else "optional"))
        for path in sorted(set(f1) & set(f2)):
            a, b = f1[path], f2[path]
            if a["type"] != b["type"]:
                out.append(Candidate("type_changed", name, path, old=a["type"], new=b["type"]))
            if a["required"] != b["required"]:
                out.append(Candidate("required_changed", name, path, old=a["required"], new=b["required"]))
            if a["const"] != b["const"] and (a["const"] is not None or b["const"] is not None):
                out.append(Candidate("value_changed", name, path, old=a["const"], new=b["const"], detail="const"))
            if a["enum"] != b["enum"] and (a["enum"] or b["enum"]):
                out.append(Candidate("value_changed", name, path, old=a["enum"], new=b["enum"], detail="enum"))
            x1, x2 = ex1.get(name, {}).get(path), ex2.get(name, {}).get(path)
            if "." not in path and isinstance(x1, (str, int, float, bool)) and x1 != x2 and x2 is not None \
                    and a["const"] is None:
                out.append(Candidate("example_changed", name, path, old=x1, new=x2, detail="response example"))
    return out


def skeleton(candidates: list[Candidate], v1: dict | None, v2: dict | None) -> DriftSpec:
    """The structure the model may annotate but not change."""
    info1, info2 = (v1 or {}).get("info", {}), (v2 or {}).get("info", {})
    spec = DriftSpec(api=info2.get("title") or info1.get("title") or "unknown API",
                     from_version=_version_tag(v1) or "v1", to_version=_version_tag(v2) or "v2")
    for c in candidates:
        if c.kind == "endpoint_renamed":
            m, old = c.old.split(" ", 1)
            spec.endpoints_renamed.append(EndpointRename(method=m, old=old, new=c.new.split(" ", 1)[1]))
        elif c.kind == "field_removed":
            spec.fields_removed.append(FieldRemoved(schema_name=c.schema_name, path=c.path, old_type=str(c.old)))
        elif c.kind == "field_added":
            spec.fields_added.append(FieldAdded(schema_name=c.schema_name, path=c.path, type=str(c.new),
                                                required=c.detail == "required"))
        elif c.kind == "type_changed":
            spec.type_changes.append(TypeChange(schema_name=c.schema_name, path=c.path, old_type=str(c.old),
                                                new_type=str(c.new)))
        elif c.kind == "value_changed" and c.old is not None and c.new is not None:
            spec.value_changes.append(ValueChange(schema_name=c.schema_name, path=c.path, old=_scalar(c.old),
                                                  new=_scalar(c.new)))
    return spec


def _scalar(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def _version_tag(doc: dict | None) -> str | None:
    if not doc:
        return None
    seg = _server_prefix(doc).rsplit("/", 1)[-1]
    if VERSION_SEG.match(seg):
        return seg
    v = str(doc.get("info", {}).get("version", ""))
    return f"v{v.split('.')[0]}" if v[:1].isdigit() else None


# --------------------------------------------------------------------------- pass 2: model merge

def strict_schema(model: type) -> dict:
    """DriftSpec's JSON schema made strict-mode compatible: every property required, no extras, no defaults."""
    schema = copy.deepcopy(model.model_json_schema())

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    return schema


INSTRUCTIONS = """You turn an upstream API change into a DriftSpec: a precise, machine-readable list of breaking
changes that a repair agent will implement. You merge two sources:

1. CANDIDATES: a deterministic diff of the two OpenAPI documents, already shaped as a DriftSpec skeleton.
   These entries are facts. Keep every one of them, with the same schema_name, path, method, old/new and
   types. Do not drop, merge, rename or re-path them.
2. CHANGELOG: the provider's release notes.

Your job is to annotate and complete:
- fields_removed[].replacement: what replaces the removed field (e.g. "orientation.{qx,qy,qz,qw}", "covariance").
- notes on any entry: migration formulas and semantics stated in the changelog, verbatim where possible
  (e.g. "yaw = atan2(2(qw·qz + qx·qy), 1 − 2(qy² + qz²))", "sigma = sqrt(trace(P_pos)/3)"), sign conventions,
  units, thresholds (e.g. "alert below 0.5"). Put each formula on the entry it applies to.
- value_changes: constant/enum/version values the changelog states changed (e.g. api_version "v1" -> "v2").
- anything else the changelog states that the spec diff cannot see (renamed fields, changed units or
  semantics with an unchanged schema). Add those as new entries.
- summary: 2-4 sentences covering every breaking change.
- target_dependency: if, and only if, the changelog explicitly states the package to install AND a version
  constraint for it (e.g. "requires foo>=2,<3"), fill name and constraint (constraint in pip syntax, e.g. ">=2,<3")
  and set source to "changelog". An instruction to upgrade without a version, or a version only in a title, is not a
  stated constraint: use null.
Use dotted paths inside the named schema (schema_name="TelemetryFrame", path="orientation.roll").
Never invent a change that neither source supports. Use "" for notes you have nothing to say about."""


@dataclass
class DiffResult:
    spec: DriftSpec
    candidates: list[Candidate]
    skeleton: DriftSpec
    model_added: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    cost: Cost = field(default_factory=Cost)
    format_used: str = "json_schema"
    latency_s: float = 0.0


def _key(category: str, e: Any) -> tuple:
    if category == "endpoints_renamed":
        return (e.method.upper(), e.old)
    if category == "fields_renamed":
        return (e.schema_name, e.old_path)
    return (e.schema_name, e.path)


ANNOTATIONS = {"fields_removed": ("replacement", "notes"), "fields_added": ("notes",), "fields_renamed": ("notes",),
               "type_changes": ("notes",), "value_changes": ("notes",), "endpoints_renamed": ("notes",)}
CATEGORIES = tuple(ANNOTATIONS)


def merge(skel: DriftSpec, model_spec: DriftSpec) -> tuple[DriftSpec, list[str], list[str]]:
    """Deterministic entries keep their structure; take only annotations from the model; keep model additions."""
    out = skel.model_copy(deep=True)
    out.summary = model_spec.summary or skel.summary
    if model_spec.target_dependency is not None and model_spec.target_dependency.constraint.strip():
        out.target_dependency = model_spec.target_dependency.model_copy(update={"source": "changelog"})
    if skel.api == "unknown API":
        # No OpenAPI documents: the model names the API, but version labels never come from it (wolverine run 4
        # labelled openai 0.x -> 1.x as "v1 -> v2"); resolve_versions fills them from the CLI or --target-dep.
        out.api, out.from_version, out.to_version = model_spec.api, "", ""
    added, restored = [], []
    for cat in CATEGORIES:
        mine = getattr(out, cat)
        theirs = {_key(cat, e): e for e in getattr(model_spec, cat)}
        keys = {_key(cat, e) for e in mine}
        for e in mine:
            m = theirs.get(_key(cat, e))
            if m is None:
                restored.append(f"{cat}:{'.'.join(map(str, _key(cat, e)))}")
                continue
            for attr in ANNOTATIONS[cat]:
                val = getattr(m, attr)
                if val not in (None, ""):
                    setattr(e, attr, val)
        for k, e in theirs.items():
            if k not in keys:
                mine.append(e)
                added.append(f"{cat}:{'.'.join(map(str, k))}")
    return out, added, restored


def build_prompt(candidates: list[Candidate], skel: DriftSpec, changelog: str | None) -> str:
    parts = ["## CANDIDATES (deterministic OpenAPI diff)", "",
             "Raw diff, including context that is not part of the skeleton (schemas added/removed, example values):",
             "```json", json.dumps([c.as_dict() for c in candidates], indent=1, default=str), "```", "",
             "DriftSpec skeleton built from it (keep every entry):",
             "```json", skel.model_dump_json(indent=1), "```", ""]
    parts += ["## CHANGELOG", "", changelog.strip() if changelog else "(none provided)", "",
              "Return the completed DriftSpec."]
    return "\n".join(parts)


def diff(changelog: str | Path | None = None, openapi_v1: str | Path | None = None,
         openapi_v2: str | Path | None = None, *, client=None, model: str = DIFF_MODEL,
         log_path: Path | None = None) -> DiffResult:
    if not (changelog or (openapi_v1 and openapi_v2)):
        raise ValueError("need --changelog, or both --openapi-v1 and --openapi-v2")
    v1 = json.loads(Path(openapi_v1).read_text()) if openapi_v1 else None
    v2 = json.loads(Path(openapi_v2).read_text()) if openapi_v2 else None
    text = Path(changelog).read_text() if changelog else None
    candidates = openapi_candidates(v1, v2) if v1 and v2 else []
    skel = skeleton(candidates, v1, v2)

    fmt = {"type": "json_schema", "name": "DriftSpec", "strict": True, "schema": strict_schema(DriftSpec)}
    r = providers.respond(build_prompt(candidates, skel, text), instructions=INSTRUCTIONS, model=model,
                          max_output_tokens=DIFF_MAX_OUTPUT_TOKENS, text_format=fmt, client=client, log_path=log_path)
    try:
        model_spec = DriftSpec.model_validate_json(r.text)
    except ValidationError as exc:  # e.g. output cut off at max_output_tokens: say so instead of a raw traceback
        raise providers.ProviderError(
            f"{model} returned an invalid DriftSpec (status={r.status}, output_tokens={r.output_tokens}/"
            f"{DIFF_MAX_OUTPUT_TOKENS}, ${r.usd or 0:.4f}): {exc.errors()[0]['msg']}") from exc
    spec, added, restored = merge(skel, model_spec)
    cost = Cost(r.input_tokens, r.cached_tokens, r.output_tokens, r.reasoning_tokens, r.usd)
    return DiffResult(spec=spec, candidates=candidates, skeleton=skel, model_added=added, restored=restored,
                      cost=cost, latency_s=r.latency_s)


def parse_target_dep(text: str):
    """`openai>=1,<2` -> TargetDependency(name='openai', constraint='>=1,<2', source='cli')."""
    from packaging.requirements import Requirement

    from driftzero.spec import TargetDependency

    req = Requirement(text)
    constraint = ",".join(sorted(str(sp) for sp in req.specifier)) if req.specifier else ""
    if not constraint:
        raise ValueError(f"--target-dep needs a version constraint, got {text!r}")
    return TargetDependency(name=req.name, constraint=constraint, source="cli")


def versions_from_requirement(requirement: str) -> tuple[str | None, str | None]:
    """Version labels implied by a target pin: "openai>=1,<2" -> ("<1", "1.x"); (None, None) without a lower bound."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    lower = op = None
    uppers = []
    for s in Requirement(requirement).specifier:
        if s.operator in (">=", "==", "~=", ">"):
            lower, op = s.version, s.operator
        elif s.operator in ("<", "<="):
            uppers.append(Version(s.version))
    if lower is None:
        return None, None
    major = Version(lower).major
    if op == "~=":  # ~=1.4 means >=1.4,<2; ~=1.4.2 means >=1.4.2,<1.5
        rel = Version(lower).release
        uppers.append(Version(".".join(map(str, (*rel[:-2], rel[-2] + 1)))))
    if op == "==":
        return f"<{lower}", lower
    if uppers and min(uppers) <= Version(f"{major + 1}"):
        return f"<{lower}", f"{major}.x"
    return f"<{lower}", f">={lower}"


def resolve_versions(spec: DriftSpec, from_cli: str | None, to_cli: str | None) -> tuple[DriftSpec, str]:
    """Version labels from --from-version/--to-version, else derived from the target dependency, else the
    spec's own (OpenAPI version tags or a hand-written spec). Never from the differ's model."""
    derived = versions_from_requirement(spec.target_dependency.requirement) if spec.target_dependency else (None, None)
    frm = from_cli or derived[0] or spec.from_version
    to = to_cli or derived[1] or spec.to_version
    if not (frm and to):
        raise ValueError("no version labels: the changelog alone does not set them; pass --from-version/--to-version "
                         "or --target-dep")
    sources = []
    if from_cli or to_cli:
        sources.append(" and ".join(f for f, v in (("--from-version", from_cli), ("--to-version", to_cli)) if v))
    if any(d and not c for d, c in ((derived[0], from_cli), (derived[1], to_cli))):
        sources.append("the target dependency")
    if (not from_cli and not derived[0]) or (not to_cli and not derived[1]):
        sources.append("the spec")
    return spec.model_copy(update={"from_version": frm, "to_version": to}), ", ".join(sources)


def resolve_target_dependency(spec: DriftSpec, cli: str | None) -> DriftSpec:
    """Changelog-stated dependency if present, else --target-dep; a disagreement between the two is an error."""
    if cli is None:
        return spec
    from_cli = parse_target_dep(cli)
    stated = spec.target_dependency
    if stated is None:
        return spec.model_copy(update={"target_dependency": from_cli})
    from packaging.specifiers import SpecifierSet

    same = stated.name.lower() == from_cli.name.lower() and \
        SpecifierSet(stated.constraint) == SpecifierSet(from_cli.constraint)
    if not same:
        raise ValueError(f"the changelog states {stated.requirement} but --target-dep is {from_cli.requirement}; "
                         "fix one of them")
    return spec


# --------------------------------------------------------------------------- comparison

def compare(generated: DriftSpec, reference: DriftSpec) -> dict[str, dict[str, list]]:
    """Per category: keys only in the generated spec, only in the reference, and annotation mismatches."""
    report: dict[str, dict[str, list]] = {}
    for cat in CATEGORIES:
        g = {_key(cat, e): e for e in getattr(generated, cat)}
        r = {_key(cat, e): e for e in getattr(reference, cat)}
        entry = {"only_generated": sorted(map(list, set(g) - set(r))), "only_reference": sorted(map(list, set(r) - set(g))),
                 "replacement_differs": []}
        if cat == "fields_removed":
            for k in set(g) & set(r):
                if (g[k].replacement or "") != (r[k].replacement or ""):
                    entry["replacement_differs"].append([list(k), g[k].replacement, r[k].replacement])
        report[cat] = entry
    return report


def format_comparison(report: dict) -> str:
    lines = []
    for cat, e in report.items():
        if not any(e.values()):
            lines.append(f"{cat}: match")
            continue
        for k in e["only_generated"]:
            lines.append(f"{cat}: only in generated: {'.'.join(map(str, k))}")
        for k in e["only_reference"]:
            lines.append(f"{cat}: only in hand-written: {'.'.join(map(str, k))}")
        for k, a, b in e["replacement_differs"]:
            lines.append(f"{cat}: replacement differs at {'.'.join(map(str, k))}: generated {a!r} vs hand-written {b!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]

    def opt(name: str) -> str | None:
        return args[args.index(name) + 1] if name in args else None

    res = diff(opt("--changelog"), opt("--openapi-v1"), opt("--openapi-v2"))
    print(res.spec.model_dump_json(indent=2))
    print(f"model added {res.model_added}; restored {res.restored}; ${res.cost.usd:.4f}", file=sys.stderr)
