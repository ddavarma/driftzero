"""v2-specific behaviour: health alerts, quaternion sign invariance, covariance handling."""
import copy
import math

import numpy as np
import pytest

from fleet_telemetry.alerts import MIN_HEALTH, check_frame
from fleet_telemetry.client import TelemetryParseError, parse_frame
from fleet_telemetry.models import Pose
from fleet_telemetry.orientation import heading_deg, rotation_matrix, tilt_deg


def kinds(frame):
    return {a.kind for a in check_frame(frame)}


def test_min_health_threshold_value():
    assert MIN_HEALTH == 0.5


def test_health_alert_only_below_threshold(frames):
    assert "health" in kinds(frames["amr-03"])  # health 0.41
    assert "health" not in kinds(frames["amr-01"])
    assert "health" not in kinds(frames["amr-02"])
    assert "health" not in kinds(frames["amr-04"])


def test_health_boundary_is_exclusive(fleet_payload):
    raw = copy.deepcopy(fleet_payload["frames"][0])
    raw["health"] = 0.5
    assert "health" not in kinds(parse_frame(raw))
    raw["health"] = 0.4999
    frame = parse_frame(raw)
    assert "health" in kinds(frame)
    assert frame.robot_id == "amr-01"


def test_health_threshold_is_overridable(frames):
    assert "health" not in kinds(frames["amr-01"])
    assert {a.kind for a in check_frame(frames["amr-01"], min_health=0.99)} == {"health"}
    assert kinds(frames["amr-03"]) == {"noisy", "health"}
    assert {a.kind for a in check_frame(frames["amr-03"], min_health=0.1)} == {"noisy"}


def test_health_alert_carries_robot_id(frames):
    alerts = [a for a in check_frame(frames["amr-03"]) if a.kind == "health"]
    assert len(alerts) == 1
    assert alerts[0].robot_id == "amr-03"


def test_negative_qw_heading_359(frames):
    pose = frames["amr-03"].pose
    assert pose.qw is not None and pose.qw < 0
    assert pose.qw == pytest.approx(-0.99993, abs=1e-4)
    assert heading_deg(pose) == pytest.approx(359.0)


def test_negated_quaternion_gives_same_rotation():
    q = (0.006958, -0.002679, 0.008708, -0.999934)
    pos = Pose(15.2, 7.7, 0.1, 0.0, 0.0, 0.0, qx=q[0], qy=q[1], qz=q[2], qw=q[3])
    neg = Pose(15.2, 7.7, 0.1, 0.0, 0.0, 0.0, qx=-q[0], qy=-q[1], qz=-q[2], qw=-q[3])
    assert np.array_equal(rotation_matrix(pos), rotation_matrix(neg))
    assert heading_deg(pos) == heading_deg(neg)
    assert tilt_deg(pos) == tilt_deg(neg)


def test_quaternion_rotation_matches_euler_convention(frames):
    # amr-01 filming pose: v1 Euler was roll 0.5, pitch -1.2, yaw 87.0
    r, p, y = np.radians([0.5, -1.2, 87.0])
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    expected = (
        np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
        @ np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
        @ np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    )
    assert np.allclose(rotation_matrix(frames["amr-01"].pose), expected, atol=1e-4)


def test_covariance_shape_is_validated(fleet_payload):
    raw = copy.deepcopy(fleet_payload["frames"][0])
    del raw["covariance"]
    with pytest.raises(TelemetryParseError, match="covariance"):
        parse_frame(raw)
    for bad in ([[0.0] * 6] * 5, [[0.0] * 5] * 6, "nope", [[0.0] * 6] * 5 + [["x"] * 6]):
        raw = copy.deepcopy(fleet_payload["frames"][0])
        raw["covariance"] = bad
        with pytest.raises(TelemetryParseError, match="covariance"):
            parse_frame(raw)


def test_sigma_uses_position_block_trace(fleet_payload):
    raw = copy.deepcopy(fleet_payload["frames"][0])
    cov = [[0.0] * 6 for _ in range(6)]
    cov[0][0], cov[1][1], cov[2][2] = 0.01, 0.04, 0.09
    cov[3][3] = cov[4][4] = cov[5][5] = 1e-4
    raw["covariance"] = cov
    frame = parse_frame(raw)
    assert frame.noise == pytest.approx(math.sqrt((0.01 + 0.04 + 0.09) / 3.0))
    assert len(frame.covariance) == 6
    assert all(len(row) == 6 for row in frame.covariance)


def test_zero_covariance_rejected(fleet_payload):
    raw = copy.deepcopy(fleet_payload["frames"][0])
    raw["covariance"] = [[0.0] * 6 for _ in range(6)]
    with pytest.raises(TelemetryParseError, match="sigma"):
        parse_frame(raw)


def test_missing_health_rejected(fleet_payload):
    raw = copy.deepcopy(fleet_payload["frames"][0])
    del raw["health"]
    with pytest.raises(TelemetryParseError, match="health"):
        parse_frame(raw)
