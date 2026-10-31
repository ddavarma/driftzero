"""Changes for external targets: per-target config, install plans, target dependency, stub guard,
collection-error retries, blast radius for unittest/mock.patch code."""
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from driftzero import blast_radius, differ, env, guard, loop, repair, target, verify
from driftzero.spec import DriftSpec, FieldRenamed, TargetDependency

ROOT = Path(__file__).resolve().parents[1]
FLEET = ROOT / "benchmarks" / "robotics_telemetry_drift"
FLEET_SPEC = DriftSpec.load(FLEET / "driftspec_v2.json")


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return root


# ---------------------------------------------------------------- (1) install plans and target config


def test_install_plan_strict_and_lenient(tmp_path):
    proj = write(tmp_path / "p", {"requirements.txt": "openai==0.28.1\ntiktoken==0.5.1  # tokenizer\n-r other.txt\n",
                                  "requirements-dev.txt": "pytest-mock\n"})
    strict = env.install_plan(proj, "openai>=1,<2", ["respx"])
    assert strict == [["-r", "requirements-dev.txt", "-r", "requirements.txt", "openai>=1,<2"], ["pytest", "respx"]]
    lenient = env.install_plan(proj, "openai>=1,<2", lenient=True)
    assert lenient[0] == ["pytest-mock", "tiktoken==0.5.1", "openai>=1,<2"]  # the old openai pin is dropped
    pkg = write(tmp_path / "q", {"pyproject.toml": '[project]\nname="q"\nversion="0"\ndependencies=["numpy>=1.24", "openai<1"]\n'})
    assert env.install_plan(pkg, "openai>=1,<2") == [["-e", ".", "openai>=1,<2"], ["pytest"]]
    assert env.install_plan(pkg, "openai>=1,<2", lenient=True)[:2] == [["numpy>=1.24", "openai>=1,<2"],
                                                                      ["--no-deps", "-e", "."]]
    bare = write(tmp_path / "r", {"x.py": ""})
    assert env.install_plan(bare, "openai>=1,<2") == [["openai>=1,<2"], ["pytest"]]
    assert env.install_plan(bare) == [["pytest"]]


def test_target_config_by_slug_by_benchmark_path_and_fallback(tmp_path):
    jt = target.config_for(target.Target(path=tmp_path, slug="ddavarma/JobTracker-driftzero-test"), ROOT)
    assert jt.holdout == ROOT / "benchmarks/jobtracker_openai_v1/holdout_v2" and jt.holdout_name == "holdout_v2"
    assert jt.holdout_requirements and jt.validated_against == "openai>=1,<2" and jt.repair_rules == []
    wv = target.config_for(target.Target(path=tmp_path, slug="ddavarma/wolverine-driftzero-test"), ROOT)
    assert wv.holdout == ROOT / "benchmarks/wolverine_openai_v1/holdout_v2" and wv.holdout.is_dir()
    assert wv.validated_against == "openai>=1,<2" and len(wv.repair_rules) == 1
    fleet = target.config_for(target.Target(path=FLEET.resolve()), ROOT)
    assert fleet.holdout == FLEET / "holdout_v2" and any("health alert" in r for r in fleet.repair_rules)
    other = write(tmp_path / "o", {"holdout_x/test_a.py": "def test_a(): pass\n"})
    fb = target.config_for(target.Target(path=other.resolve()), ROOT)
    assert fb.holdout == (other / "holdout_x").resolve() and fb.holdout_name == "holdout_x" and fb.repair_rules == []


def test_holdout_name_used_in_commands_and_tarball(tmp_path):
    _, cmd = verify.pytest_commands("/tmp", "contract_tests")
    assert cmd[-1] == "contract_tests/"
    wt = write(tmp_path / "wt", {"a.py": ""})
    ho = write(tmp_path / "hold", {"test_h.py": "def test_h(): pass\n"})
    import io
    import tarfile

    names = tarfile.open(fileobj=io.BytesIO(verify._tar_bytes(wt, ho, ".", "contract_tests"))).getnames()
    assert "wt/./contract_tests/test_h.py" in names or "wt/contract_tests/test_h.py" in names


# ---------------------------------------------------------------- (2) target dependency


def test_parse_and_resolve_target_dependency():
    td = differ.parse_target_dep("openai>=1,<2")
    assert (td.name, td.constraint, td.source, td.requirement) == ("openai", "<2,>=1", "cli", "openai<2,>=1")
    with pytest.raises(ValueError):
        differ.parse_target_dep("openai")
    spec = FLEET_SPEC.model_copy()
    assert differ.resolve_target_dependency(spec, "openai>=1,<2").target_dependency.source == "cli"
    stated = spec.model_copy(update={"target_dependency": TargetDependency(name="openai", constraint=">=1,<2",
                                                                           source="changelog")})
    assert differ.resolve_target_dependency(stated, "openai<2,>=1").target_dependency.source == "changelog"
    with pytest.raises(ValueError, match="changelog states"):
        differ.resolve_target_dependency(stated, "openai>=1")
    assert differ.resolve_target_dependency(spec, None) is spec


def test_merge_takes_changelog_dependency_and_schema_allows_none():
    skel = DriftSpec(api="x", from_version="0", to_version="1")
    model = skel.model_copy(update={"target_dependency": TargetDependency(name="foo", constraint=">=2,<3")})
    merged, _, _ = differ.merge(skel, model)
    assert merged.target_dependency.requirement == "foo>=2,<3" and merged.target_dependency.source == "changelog"
    assert differ.merge(skel, skel)[0].target_dependency is None
    schema = json.dumps(differ.strict_schema(DriftSpec))
    assert '"target_dependency"' in schema and '"null"' in schema


def test_hard_rules_are_generic_plus_target_rules():
    generic = "\n".join(repair.hard_rules(FLEET_SPEC))
    for leak in ("health", "qw", "covariance", "fleet", "numpy"):
        assert leak not in generic.lower(), leak
    spec = FLEET_SPEC.model_copy(update={"target_dependency": TargetDependency(name="openai", constraint=">=1,<2")})
    rules = repair.hard_rules(spec, ".", "/v/bin/python", ["Target rule for {to_version}."])
    assert any("must depend on `openai>=1,<2`" in r for r in rules)
    assert any("Never stub, mock or make `openai` optional" in r for r in rules)
    assert rules[-1].endswith("Target rule for v2.") and rules[-1].startswith(f"{len(rules)}. ")
    assert any("/v/bin/python -m pytest -q" in r for r in rules)


# ---------------------------------------------------------------- (3) collection errors in the retry prompt


def test_collection_errors_scrubbed_into_retry_prompt():
    out = ("ImportError while importing test module '/work/wt/holdout_v2/test_contract_openai_v1.py'.\n"
           "holdout_v2/test_contract_openai_v1.py:17: in <module>\n    import httpx\n"
           "E   ModuleNotFoundError: No module named 'httpx'\n"
           "ERROR holdout_v2/test_contract_openai_v1.py\n> def secret_body(): assert x\n")
    h = verify.SuiteResult(name="holdout", command=[], exit_code=2, stdout=out, stderr="", junit_xml=None)
    a = h.model_copy(update={"name": "agent", "exit_code": 0, "stdout": "5 passed", "tests": 5})
    res = verify.VerifyResult(passed=False, backend="modal", project_rel=".", agent=a, holdout=h,
                              setup={"exit_code": 0}, wall_time_s=1)
    text = loop.build_prior_failure(res, "holdout_v2")
    assert "could not run (pytest exit 2" in text and "E   ModuleNotFoundError: No module named 'httpx'" in text
    for leak in ("holdout_v2", "test_contract_openai_v1", "/work/wt", "secret_body", "import httpx"):
        assert leak not in text, leak



def test_retry_prompt_names_the_install_step_that_failed():
    cmd = ["python", "-m", "pip", "install", "-r", "requirements.txt", "openai<2,>=1"]
    h = verify.SuiteResult(name="holdout", command=[], exit_code=2, stdout="", stderr="", junit_xml=None)
    res = verify.VerifyResult(passed=False, backend="modal", project_rel=".", agent=h, holdout=h, wall_time_s=1,
                              setup={"exit_code": 1, "output": "ResolutionImpossible", "command": [cmd],
                                     "steps": [{"command": cmd, "exit_code": 1, "output": "ResolutionImpossible"}]})
    text = loop.build_prior_failure(res, "holdout_v2")
    assert "### Dependency install failed: `python -m pip install -r requirements.txt openai<2,>=1`" in text
    assert "ResolutionImpossible" in text and "pip install -e ." not in text

# ---------------------------------------------------------------- (4) guard: dependency stubbing in library code


STUB = '''\
try:
    from openai import OpenAI, OpenAIError
except ImportError:
    class OpenAIError(Exception):
        pass
'''


@pytest.mark.parametrize("src,what", [
    (STUB, "try/except ImportError"),
    ("try:\n    import openai.types as t\nexcept (ModuleNotFoundError, OSError):\n    t = None\n", "try/except ImportError"),
    ("import sys, types\nsys.modules['openai'] = types.ModuleType('openai')\n", "sys.modules['openai'] assignment"),
    ("import sys\nsys.modules.setdefault('openai.types', object())\n", "sys.modules.setdefault"),
    ("import sys\nsys.modules.update({'openai': None})\n", "sys.modules.update"),
    ("import sys\nfrom unittest import mock\nwith mock.patch.dict(sys.modules, {'openai': None}):\n    pass\n", "patch.dict"),
])
def test_guard_rejects_dependency_stubs_in_library_code(src, what):
    res = guard.check_changes([guard.FileChange("src/pkg/client.py", "import openai\n", src)], "openai")
    assert not res.ok and res.rejections[0].rule == "dependency_stubbed" and what in res.rejections[0].message


def test_guard_dependency_stub_scope():
    # tests may mock; pre-existing fallbacks are not the repair's doing; other packages and no target are ignored
    assert guard.check_changes([guard.FileChange("tests/test_c.py", "", STUB)], "openai").ok
    assert guard.check_changes([guard.FileChange("src/c.py", STUB, STUB + "\nx = 1\n")], "openai").ok
    assert guard.check_changes([guard.FileChange("src/c.py", "", STUB.replace("openai", "yaml"))], "openai").ok
    assert guard.check_changes([guard.FileChange("src/c.py", "", STUB)], None).ok
    assert guard.check_changes([guard.FileChange("src/c.py", "", "import openai\nclient = openai.OpenAI()\n")],
                               "openai").ok


def test_guard_rejects_the_stub_from_the_failed_jobtracker_run():
    diff = (ROOT / "docs" / "runs" / "20261030-165401-ebb8d2.i3.diff").read_text()
    assert "except ImportError:" in diff
    first_hunk = diff.split("+++ b/src/JobTracker/chatbot.py", 1)[1].split("\n@@ ", 2)[1]
    new = "\n".join(l[1:] for l in first_hunk.splitlines()[1:] if l.startswith((" ", "+")))
    assert guard.dependency_stubs(new, "openai") == ["line 5: try/except ImportError around `import openai`"]


# ---------------------------------------------------------------- (5) blast radius: dotted strings and setUp


def test_blast_radius_links_unittest_setup_patch_strings_and_src_layout(tmp_path):
    repo = write(tmp_path / "repo", {
        "src/__init__.py": "", "src/app/__init__.py": "",
        "src/app/bot.py": """
            import openai

            class Bot:
                def __init__(self):
                    self.models = openai.Model.list()

                def ask(self, q):
                    return q
        """,
        "src/app/plain.py": "def helper():\n    return 1\n",
        "tests/test_bot.py": """
            import unittest
            from unittest.mock import patch
            from src.app.bot import Bot

            class TestBot(unittest.TestCase):
                @patch("openai.Model.list")
                def setUp(self, m):
                    self.bot = Bot()

                def test_ask(self):
                    self.assertEqual(self.bot.ask("x"), "x")

            class TestPatchTarget(unittest.TestCase):
                @patch("src.app.bot.Bot")
                def test_uses_string_target(self, b):
                    pass

            class TestPlain(unittest.TestCase):
                def test_helper(self):
                    from src.app.plain import helper
                    self.assertEqual(helper(), 1)
        """,
    })
    spec = DriftSpec(api="openai", from_version="0", to_version="1",
                     fields_renamed=[FieldRenamed(schema_name="openai", old_path="Model.list", new_path="models.list")])
    br = blast_radius.analyze(repo, spec)
    ids = {s.id: s for s in br.symbols}
    setup = ids["tests/test_bot.py::TestBot.setUp"]
    assert setup.direct and ("dotted_string", "openai.Model.list") in {(m.kind, m.text) for m in setup.matches}
    assert "tests/test_bot.py::TestBot::test_ask" in br.tests                # via setUp
    assert "tests/test_bot.py::TestPatchTarget::test_uses_string_target" in br.tests  # via the patch-target string
    assert "tests/test_bot.py::TestPlain::test_helper" not in br.tests
    assert any(e.kind == "fixture" and e.detail == "setUp" for e in br.edges)
    assert any(e.kind == "ref" and e.detail == "string" and e.dst == "src/app/bot.py::Bot" for e in br.edges)


def test_validated_against_compares_requirements_not_strings():
    assert loop.same_requirement("openai>=1,<2", "openai<2,>=1")
    assert loop.same_requirement("OpenAI>=1,<2", "openai >=1, <2")
    assert not loop.same_requirement("openai>=1,<2", "openai>=1")
    assert not loop.same_requirement("openai>=1,<2", "httpx>=1,<2")


# ---------------------------------------------------------------- caches and binary files in the worktree (wolverine)


def test_caches_excluded_from_diff_and_guard_survives_binary_files(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                       capture_output=True)

    root = write(tmp_path / "r", {".gitignore": "venv\n", "pkg/mod.py": "x = 1\n", "tests/test_m.py": "def test_m():\n    assert True\n"})
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    wt = repair.create_worktree(root, "c")
    try:
        (wt.path / "pkg" / "mod.py").write_text("x = 2\n")
        for rel in ("pkg/__pycache__/mod.cpython-311.pyc", "tests/__pycache__/test_m.cpython-311-pytest-7.3.1.pyc"):
            (wt.path / rel).parent.mkdir(parents=True)
            (wt.path / rel).write_bytes(b"\xa7\r\r\n\x00binary")
        (wt.path / "pkg" / "blob.bin").write_bytes(b"\xa7\x00\xff")
        diff = repair.collect_diff(wt)
        assert {p for p, _, _ in diff.files} == {"pkg/mod.py", "pkg/blob.bin"}
        res = guard.guard_check(wt, diff)  # used to raise UnicodeDecodeError on the binary file
        assert res.ok
    finally:
        repair.remove_worktree(wt)
