import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from driftzero.blast_radius import analyze
from driftzero.cli import app
from driftzero.spec import DriftSpec, FieldRemoved, FieldRenamed

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "robotics_telemetry_drift"
SPEC_PATH = BENCH / "driftspec_v2.json"

PKG = "fleet_telemetry/"
IMPACTED_FILES = {
    PKG + "models.py",
    PKG + "client.py",
    PKG + "orientation.py",
    PKG + "fusion.py",
    PKG + "alerts.py",
    PKG + "report.py",
    "tests/conftest.py",
    "tests/test_client.py",
    "tests/test_orientation.py",
    "tests/test_fusion.py",
    "tests/test_alerts.py",
}
DIRECT = {
    PKG + "models.py::Pose",
    PKG + "models.py::TelemetryFrame",
    PKG + "client.py::API_VERSION",
    PKG + "client.py::BASE_PATH",
    PKG + "client.py::parse_frame",
    PKG + "client.py::parse_batch",
    PKG + "orientation.py::rotation_matrix",
    PKG + "orientation.py::heading_deg",
    PKG + "fusion.py::position_variance",
    PKG + "fusion.py::fuse_heading",
    PKG + "alerts.py::check_frame",
}
TRANSITIVE = {
    PKG + "client.py::FleetClient.latest",
    PKG + "client.py::FleetClient.fleet_snapshot",
    PKG + "orientation.py::heading_vector",
    PKG + "orientation.py::tilt_deg",
    PKG + "fusion.py::fuse_positions",
    PKG + "report.py::summarize",
    "tests/conftest.py::fleet_payload",
    "tests/conftest.py::frames",
    "tests/conftest.py::client",
}
UNRELATED = {
    PKG + "__init__.py",
    PKG + "charging.py",
    "tests/test_charging.py",
}
UNRELATED_SYMBOLS = {
    PKG + "alerts.py::Alert",
    PKG + "alerts.py::MIN_BATTERY",
    PKG + "client.py::TelemetryParseError",
    PKG + "client.py::Transport",
    PKG + "report.py::FleetSummary",
    PKG + "report.py::FleetSummary.healthy_ids",
    PKG + "report.py::format_summary",
    PKG + "charging.py::plan_charging",
    PKG + "charging.py::charge_minutes",
    "tests/test_charging.py::test_charge_minutes",
}


def bench_tests() -> set[str]:
    """Every test function in the benchmark, as pytest node ids."""
    import ast

    ids = set()
    for path in (BENCH / "tests").glob("test_*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                ids.add(f"tests/{path.name}::{node.name}")
    return ids


@pytest.fixture(scope="module")
def br():
    return analyze(BENCH, DriftSpec.load(SPEC_PATH))


def edge(br, src, dst, kind):
    return any(e.src == src and e.dst == dst and e.kind == kind for e in br.edges)


# ---------------------------------------------------------------- DriftSpec


def test_benchmark_driftspec_loads_and_round_trips(tmp_path):
    spec = DriftSpec.load(SPEC_PATH)
    assert (spec.from_version, spec.to_version) == ("v1", "v2")
    assert {f.path for f in spec.fields_removed} == {"orientation.roll", "orientation.pitch", "orientation.yaw", "noise"}
    assert {f.path for f in spec.fields_added} >= {"covariance", "health", "orientation.qw"}
    assert [e.new for e in spec.endpoints_renamed] == ["/v2/robots/{robot_id}/telemetry/latest", "/v2/fleet/telemetry"]
    spec.dump(tmp_path / "s.json")
    assert DriftSpec.load(tmp_path / "s.json") == spec


def test_driftspec_rejects_unknown_keys():
    raw = json.loads(SPEC_PATH.read_text())
    raw["fields_deleted"] = []
    with pytest.raises(ValueError):
        DriftSpec.model_validate(raw)


# ---------------------------------------------------------------- benchmark impact set


def test_impacted_files_exact(br):
    assert set(br.files) == IMPACTED_FILES
    assert not UNRELATED & set(br.files)


def test_direct_and_transitive_symbols(br):
    by_id = {s.id: s for s in br.symbols}
    for sid in DIRECT:
        assert sid in by_id and by_id[sid].direct, sid
    for sid in TRANSITIVE:
        assert sid in by_id and not by_id[sid].direct, sid
    assert not UNRELATED_SYMBOLS & set(by_id)


def test_every_benchmark_test_except_control_is_impacted(br):
    expected = {t for t in bench_tests() if not t.startswith("tests/test_charging.py")}
    assert set(br.tests) == expected
    assert len(expected) == 16


def test_client_paths_and_api_version(br):
    base = br.symbol(PKG + "client.py::BASE_PATH")
    # "/v1" is a prefix of both old endpoints, and of neither new one.
    assert {(m.kind, m.text) for m in base.matches} == {("endpoint", "/v1")}
    assert {m.probe for m in base.matches} == {
        "endpoint:GET /v1/robots/{robot_id}/telemetry/latest",
        "endpoint:GET /v1/fleet/telemetry",
    }
    paths_test = br.symbol("tests/test_client.py::test_client_hits_v1_paths")
    assert {m.text for m in paths_test.matches} == {"/v1/robots/amr-03/telemetry/latest", "/v1/fleet/telemetry"}
    assert br.symbol(PKG + "client.py::API_VERSION").matches[0].probe == "value:FleetSnapshot.api_version=v1"


def test_alerts_reading_noise_and_tilt(br):
    check = br.symbol(PKG + "alerts.py::check_frame")
    assert {(m.kind, m.text) for m in check.matches} == {("attribute", "noise")}
    assert PKG + "orientation.py::tilt_deg" in check.via
    assert edge(br, PKG + "alerts.py", PKG + "orientation.py::tilt_deg", "import")
    assert edge(br, PKG + "alerts.py::check_frame", PKG + "orientation.py::tilt_deg", "call")
    assert edge(br, PKG + "alerts.py::check_frame", "removed:TelemetryFrame.noise", "field_access")


def test_orientation_and_fusion_consumers(br):
    rot = br.symbol(PKG + "orientation.py::rotation_matrix")
    assert {m.text for m in rot.matches} == {"roll", "pitch", "yaw"}
    assert br.symbol(PKG + "orientation.py::heading_vector").via == [PKG + "orientation.py::rotation_matrix"]
    assert br.symbol(PKG + "fusion.py::fuse_positions").via == [PKG + "fusion.py::position_variance"]


def test_fixture_and_data_edges(br):
    assert [d.file for d in br.data_files] == ["tests/fixtures/fleet_v1.json"]
    assert edge(br, "tests/conftest.py::fleet_payload", "tests/fixtures/fleet_v1.json", "data")
    assert edge(br, "tests/test_fusion.py::test_heading_mean_handles_wraparound", "tests/conftest.py::frames", "fixture")
    assert edge(br, "tests/conftest.py::client", PKG + "client.py::FleetClient", "call")


def test_re_export_is_an_import_edge_not_an_impacted_file(br):
    assert edge(br, PKG + "__init__.py", PKG + "client.py::parse_frame", "import")
    assert PKG + "__init__.py" not in br.files


def test_deterministic(br):
    again = analyze(BENCH, DriftSpec.load(SPEC_PATH))
    assert again.model_dump_json() == br.model_dump_json()


# ---------------------------------------------------------------- resolver edge cases on a synthetic repo


def write(root: Path, rel: str, src: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))


def test_synthetic_repo_resolution(tmp_path):
    write(tmp_path, "pkg/__init__.py", "")
    write(tmp_path, "pkg/orientation.py", "def spin():\n    return 1\n")
    write(tmp_path, "pkg/api.py", """
        def read(payload):
            return payload.get("speed_kph")

        def unrelated(speed_kph=None):
            return 2
    """)
    write(tmp_path, "pkg/use.py", """
        import pkg.api as api
        import pkg.orientation
        from . import api as rel_api

        def via_alias(p):
            return api.read(p)

        def via_relative(p):
            return rel_api.read(p)

        def module_named_like_field():
            return pkg.orientation.spin()

        def shadowed(read):
            return read(1)
    """)
    spec = DriftSpec(
        api="x", from_version="1", to_version="2",
        fields_removed=[FieldRemoved(schema_name="T", path="orientation", old_type="obj")],
        fields_renamed=[FieldRenamed(schema_name="T", old_path="speed_kph", new_path="speed_mps")],
    )
    br = analyze(tmp_path, spec)
    ids = {s.id for s in br.symbols}
    assert br.symbol("pkg/api.py::read").matches[0].probe == "renamed:T.speed_kph"
    assert {"pkg/use.py::via_alias", "pkg/use.py::via_relative"} <= ids
    # `pkg.orientation` is a module path, not a field access; a parameter named after
    # a field is not a read; a local that shadows an impacted function is not a call.
    assert "pkg/use.py::module_named_like_field" not in ids
    assert "pkg/api.py::unrelated" not in ids
    assert "pkg/use.py::shadowed" not in ids


# ---------------------------------------------------------------- CLI


def test_cli_blast_table_and_json():
    runner = CliRunner()
    res = runner.invoke(app, ["blast", "--repo", str(BENCH), "--spec", str(SPEC_PATH)], env={"COLUMNS": "200"})
    assert res.exit_code == 0, res.output
    assert "check_frame" in res.output
    assert "11 files" in res.output and "16 tests" in res.output
    assert "plan_charging" not in res.output

    res = runner.invoke(app, ["blast", "--repo", str(BENCH), "--spec", str(SPEC_PATH), "--json"])
    assert res.exit_code == 0
    assert set(json.loads(res.output)["files"]) == IMPACTED_FILES
