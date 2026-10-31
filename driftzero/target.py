"""Where the code to migrate lives: a local path, or a git URL that DriftZero clones.

For a URL, the target is cloned to `<home>/.driftzero/clones/<task_id>`, the repair worktree
is cut from that clone, the branch is pushed to the clone's origin (the target repo) and the
PR is opened there. Hold-out tests never live in the target: they are looked up by the
target's `owner/name` in `benchmarks/targets.json` inside DriftZero.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TARGETS_FILE = Path(__file__).resolve().parents[1] / "benchmarks" / "targets.json"
URL_PREFIXES = ("https://", "http://", "file://", "ssh://", "git@")


@dataclass
class Target:
    path: Path              # local project directory to analyze and repair
    url: str | None = None  # remote it was cloned from
    slug: str | None = None  # owner/name for GitHub targets (gh pr create -R)
    clone: Path | None = None


def is_url(repo: str | Path) -> bool:
    return str(repo).startswith(URL_PREFIXES)


def slug_from_url(url: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1)
    return Path(url.rstrip("/")).name.removesuffix(".git")


def prepare(repo: str | Path, home: Path, task_id: str) -> Target:
    if not is_url(repo):
        return Target(path=Path(repo).resolve())
    url = str(repo).rstrip("/")
    clone = Path(home).resolve() / ".driftzero" / "clones" / task_id
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", url, str(clone)], check=True, capture_output=True, text=True)
    return Target(path=clone, url=url, slug=slug_from_url(url), clone=clone)


def holdout_for(target: Target, home: Path, targets_file: Path = TARGETS_FILE) -> Path | None:
    """Hold-out dir for a target: mapped by owner/name for remote targets, holdout_* inside a local one."""
    if target.slug:
        mapping = json.loads(Path(targets_file).read_text()) if Path(targets_file).exists() else {}
        entry = mapping.get(target.slug)
        if not entry:
            return None
        path = Path(home) / entry["holdout"]
        return path.resolve() if path.is_dir() else None
    found = sorted(p for p in target.path.glob("holdout_*") if p.is_dir())
    return found[0].resolve() if found else None


@dataclass
class TargetConfig:
    """Per-target settings from benchmarks/targets.json (all optional)."""

    holdout: Path | None = None
    holdout_name: str | None = None          # directory name the hold-outs get in the verify sandbox
    holdout_requirements: list[str] = field(default_factory=list)  # pip requirements of the hold-outs themselves
    validated_against: str | None = None     # target-dependency pin the hold-outs were validated against
    repair_rules: list[str] = field(default_factory=list)  # target-specific hard rules for the repair prompt
    holdout_description: str | None = None   # what the hold-outs are derived from, for the PR body


def config_for(target: Target, home: Path, targets_file: Path = TARGETS_FILE) -> TargetConfig:
    """Config by owner/name for remote targets, by benchmark path for local ones; else hold-outs found by glob."""
    mapping = json.loads(Path(targets_file).read_text()) if Path(targets_file).exists() else {}
    entry = mapping.get(target.slug) if target.slug else None
    if entry is None and not target.slug:
        for key, e in mapping.items():
            if isinstance(e, dict) and e.get("benchmark") and (Path(home) / e["benchmark"]).resolve() == target.path:
                entry = e
                break
    if entry is None:
        holdout = holdout_for(target, home, targets_file)
        return TargetConfig(holdout=holdout, holdout_name=holdout.name if holdout else None)
    path = (Path(home) / entry["holdout"]).resolve() if entry.get("holdout") else None
    holdout = path if path and path.is_dir() else None
    return TargetConfig(holdout=holdout, holdout_name=entry.get("holdout_name") or (holdout.name if holdout else None),
                        holdout_requirements=list(entry.get("holdout_requirements") or []),
                        validated_against=entry.get("validated_against"),
                        repair_rules=list(entry.get("repair_rules") or []),
                        holdout_description=entry.get("holdout_description"))


def home_dir() -> Path:
    """DriftZero's own checkout (where .driftzero/ state and benchmarks/ live)."""
    return Path(__file__).resolve().parents[1]
