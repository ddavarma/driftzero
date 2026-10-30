import numpy as np
import pytest

from fleet_telemetry.models import Pose
from fleet_telemetry.orientation import heading_deg, heading_vector, rotation_matrix, tilt_deg


def pose(roll=0.0, pitch=0.0, yaw=0.0):
    return Pose(0.0, 0.0, 0.0, roll, pitch, yaw)


def test_zero_angles_is_identity():
    assert np.allclose(rotation_matrix(pose()), np.eye(3))


def test_rotation_is_orthonormal(frames):
    for f in frames.values():
        r = rotation_matrix(f.pose)
        assert np.allclose(r @ r.T, np.eye(3))
        assert np.isclose(np.linalg.det(r), 1.0)


def test_yaw_90_points_along_map_y():
    assert np.allclose(heading_vector(pose(yaw=90.0)), [0.0, 1.0], atol=1e-9)


def test_heading_wraps_into_0_360(frames):
    assert heading_deg(frames["amr-04"].pose) == pytest.approx(315.0)
    assert heading_deg(frames["amr-01"].pose) == pytest.approx(87.0)


def test_tilt_from_roll_and_pitch():
    assert tilt_deg(pose(roll=20.0)) == pytest.approx(20.0)
    assert tilt_deg(pose(pitch=-10.0, yaw=123.0)) == pytest.approx(10.0)
