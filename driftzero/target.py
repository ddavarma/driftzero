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
from dataclasses import dataclass
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


def home_dir() -> Path:
    """DriftZero's own checkout (where .driftzero/ state and benchmarks/ live)."""
    return Path(__file__).resolve().parents[1]
