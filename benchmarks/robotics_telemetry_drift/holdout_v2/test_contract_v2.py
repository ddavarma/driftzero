"""Hold-out contract tests for the v2 migration.

Never shown to the repair agent: excluded from the repair prompt, from blast-radius
analysis, and hidden from the repair worktree. Derived only from openapi_v2.json and
CHANGELOG_v2.md, through the public API the v1 test-suite already used. Expected values
are computed here from the published quaternions and covariances, not copied from the
library under test.
"""
import copy
import json
import math
from pathlib import Path

import pytest

from fleet_telemetry.alerts import check_frame
from fleet_telemetry.charging import plan_charging
from fleet_telemetry.client import FleetClient, TelemetryParseError, parse_batch, parse_frame
from fleet_telemetry.orientation import heading_deg, tilt_deg

SPEC = json.loads((Path(__file__).resolve().parents[1] / "openapi_v2.json").read_text())
SNAPSHOT = SPEC["paths"]["/fleet/telemetry"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
LATEST = SPEC["paths"]["/robots/{robot_id}/telemetry/latest"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
BY_ID = {f["robot_id"]: f for f in SNAPSHOT["frames"]}


def expected_heading_tilt(q):
    """Heading and tilt implied by a scalar-last Hamilton quaternion (CHANGELOG_v2: R unchanged, q ~ -q)."""
    x, y, z, w = (q[k] for k in ("qx", "qy", "qz", "qw"))
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    r00, r10 = 1 - 2 * (y * y + z * z), 2 * (x * y + z * w)
    r22 = 1 - 2 * (x * x + y * y)
    return math.degrees(math.atan2(r10, r00)) % 360.0, math.degrees(math.acos(max(-1.0, min(1.0, r22))))


def angle_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_client_requests_v2_paths():
    calls = []

    def transport(path):
        calls.append(path)
        return SNAPSHOT if path.endswith("/fleet/telemetry") else LATEST

    client = FleetClient(transport)
    assert client.latest("amr-01").robot_id == "amr-01"
    assert len(client.fleet_snapshot()) == len(SNAPSHOT["frames"])
    assert calls == ["/v2/robots/amr-01/telemetry/latest", "/v2/fleet/telemetry"]


def test_v2_snapshot_parses_and_v1_version_is_rejected():
    frames = {f.robot_id: f for f in parse_batch(SNAPSHOT)}
    assert set(frames) == set(BY_ID)
    for rid, raw in BY_ID.items():
        f = frames[rid]
        assert (f.pose.x, f.pose.y, f.pose.z) == (raw["position"]["x"], raw["position"]["y"], raw["position"]["z"])
        assert f.battery == raw["battery"]
        assert f.timestamp == raw["timestamp"]
    stale = dict(SNAPSHOT, api_version="v1")
    with pytest.raises(TelemetryParseError):
        parse_batch(stale)


def test_negative_qw_robots_heading_and_tilt():
    negative = [rid for rid, raw in BY_ID.items() if raw["orientation"]["qw"] < 0]
    assert len(negative) == 2, "the published example has two negative-qw robots"
    for rid in negative:
        raw = BY_ID[rid]
        want_heading, want_tilt = expected_heading_tilt(raw["orientation"])
        for sign in (1, -1):  # the server may send either sign of the same rotation
            payload = copy.deepcopy(raw)
            payload["orientation"] = {k: sign * v for k, v in raw["orientation"].items()}
            pose = parse_frame(payload).pose
            assert angle_diff(heading_deg(pose), want_heading) < 0.01, (rid, sign)
            assert tilt_deg(pose) == pytest.approx(want_tilt, abs=0.01), (rid, sign)


def test_health_alert_only_below_threshold():
    healths = {rid: raw["health"] for rid, raw in BY_ID.items()}
    low = [rid for rid, h in healths.items() if h < 0.5]
    healthy = max(healths, key=healths.get)
    assert low, "the published example has a robot below the 0.5 service threshold"
    for rid in low:
        assert "health" in {a.kind for a in check_frame(parse_frame(BY_ID[rid]))}, rid
    assert "health" not in {a.kind for a in check_frame(parse_frame(BY_ID[healthy]))}


def test_noise_is_derived_from_covariance_trace():
    payload = copy.deepcopy(BY_ID["amr-01"])
    diag = [0.01, 0.04, 0.16, 1e-4, 1e-4, 1e-4]  # anisotropic position block
    payload["covariance"] = [[diag[i] if i == j else 0.0 for j in range(6)] for i in range(6)]
    payload["covariance"][0][1] = payload["covariance"][1][0] = 0.005  # off-diagonals don't enter the trace
    frame = parse_frame(payload)
    assert frame.noise == pytest.approx(math.sqrt((0.01 + 0.04 + 0.16) / 3))
    assert "noisy" in {a.kind for a in check_frame(frame)}  # sigma ~0.265 m > 0.25 m threshold


def test_v1_shaped_frame_is_rejected():
    payload = copy.deepcopy(BY_ID["amr-01"])
    del payload["covariance"]
    payload["noise"] = 0.04
    with pytest.raises(TelemetryParseError):
        parse_frame(payload)


def test_charging_schedule_unaffected():
    levels = {rid: raw["battery"] for rid, raw in BY_ID.items()}
    slots = plan_charging(levels, docks=2)
    low = sorted((b, rid) for rid, b in levels.items() if b < 0.30)
    assert [s.robot_id for s in slots] == [rid for _, rid in low]
    assert slots[0].dock == 0 and slots[0].start_min == 0.0
    assert slots[0].minutes == pytest.approx((0.95 - low[0][0]) / 0.012)


def test_health_outside_documented_range_is_rejected():
    schema = SPEC["components"]["schemas"]["TelemetryFrame"]["properties"]["health"]
    lo, hi = schema["minimum"], schema["maximum"]  # openapi_v2.json: health is 0..1
    ok = copy.deepcopy(BY_ID["amr-01"])
    assert parse_frame(ok).health == ok["health"]  # an in-range frame must parse (rules out vacuous passes)
    for bad in (hi + 1.0, lo - 0.1, float("nan")):
        payload = dict(ok, health=bad)
        with pytest.raises(TelemetryParseError):
            parse_frame(payload)
