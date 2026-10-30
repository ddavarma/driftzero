"""DriftSpec: a structured description of an upstream API change.

Produced by the differ (LLM) or written by hand; consumed by blast_radius and repair.
Field locations are a schema name plus a dotted path inside it, e.g.
schema_name="TelemetryFrame", path="orientation.roll". Endpoint paths use
OpenAPI templating ("/v1/robots/{robot_id}/telemetry/latest").
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EndpointRename(_Model):
    method: str = "GET"
    old: str
    new: str
    notes: str = ""


class FieldRemoved(_Model):
    schema_name: str
    path: str
    old_type: str
    replacement: str | None = None
    notes: str = ""


class FieldAdded(_Model):
    schema_name: str
    path: str
    type: str
    required: bool = False
    notes: str = ""


class FieldRenamed(_Model):
    schema_name: str
    old_path: str
    new_path: str
    notes: str = ""


class TypeChange(_Model):
    schema_name: str
    path: str
    old_type: str
    new_type: str
    notes: str = ""


class ValueChange(_Model):
    """A constant/enum value that changed, e.g. api_version "v1" -> "v2"."""

    schema_name: str
    path: str
    old: str
    new: str
    notes: str = ""


class TargetDependency(_Model):
    """The upgraded package the migration targets, e.g. name="openai", constraint=">=1,<2"."""

    name: str
    constraint: str
    source: str = ""  # "changelog" (stated there) or "cli" (--target-dep)

    @property
    def requirement(self) -> str:
        return f"{self.name}{self.constraint}"


class DriftSpec(_Model):
    api: str
    from_version: str
    to_version: str
    summary: str = ""
    endpoints_renamed: list[EndpointRename] = Field(default_factory=list)
    fields_removed: list[FieldRemoved] = Field(default_factory=list)
    fields_added: list[FieldAdded] = Field(default_factory=list)
    fields_renamed: list[FieldRenamed] = Field(default_factory=list)
    type_changes: list[TypeChange] = Field(default_factory=list)
    value_changes: list[ValueChange] = Field(default_factory=list)
    target_dependency: TargetDependency | None = None

    @classmethod
    def load(cls, path: str | Path) -> DriftSpec:
        return cls.model_validate_json(Path(path).read_text())

    def dump(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n")
