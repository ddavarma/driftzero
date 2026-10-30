"""Orientation math on v2 quaternion poses (Hamilton, scalar last).

The rotation is built directly from the unit quaternion, so the
formulas are sign-invariant: ``q`` and ``-q`` describe the same
rotation. Poses without a quaternion (e.g. constructed by hand from
Euler angles) fall back to the Euler convention
R = Rz(yaw) @ Ry(pitch) @ Rx(roll), degrees, intrinsic Z-Y-X.
"""
from __future__ import annotations

import math

import numpy as np

from fleet_telemetry.models import Pose


def normalize_quaternion(qx: float, qy: float, qz: float, qw: float) -> tuple[float, float, float, float]:
    """Return the unit quaternion in the direction of the input.

    Never assume the input is already unit (the server does not
    canonicalise sign or, defensively, scale).
    """
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError(f"invalid quaternion {(qx, qy, qz, qw)}")
    return (qx / norm, qy / norm, qz / norm, qw / norm)


def quaternion_to_matrix(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    """Body-to-map rotation from a (not necessarily unit) quaternion."""
    qx, qy, qz, qw = normalize_quaternion(qx, qy, qz, qw)
    return np.array(
        [
            [1.0 - 2.0 * (qy * qy + qz * qz), 2.0 * (qx * qy - qz * qw), 2.0 * (qx * qz + qy * qw)],
            [2.0 * (qx * qy + qz * qw), 1.0 - 2.0 * (qx * qx + qz * qz), 2.0 * (qy * qz - qx * qw)],
            [2.0 * (qx * qz - qy * qw), 2.0 * (qy * qz + qx * qw), 1.0 - 2.0 * (qx * qx + qy * qy)],
        ]
    )


def euler_to_quaternion_deg(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    """Quaternion (qx, qy, qz, qw) for R = Rz(yaw) @ Ry(pitch) @ Rx(roll)."""
    r, p, y = np.radians([roll, pitch, yaw])
    cr, sr = math.cos(r / 2.0), math.sin(r / 2.0)
    cp, sp = math.cos(p / 2.0), math.sin(p / 2.0)
    cy, sy = math.cos(y / 2.0), math.sin(y / 2.0)
    return (
        cy * cp * sr - sy * sp * cr,
        sy * cp * sr + cy * sp * cr,
        sy * cp * cr - cy * sp * sr,
        cy * cp * cr + sy * sp * sr,
    )


def quaternion_to_euler_deg(qx: float, qy: float, qz: float, qw: float) -> tuple[float, float, float]:
    """Euler angles (roll, pitch, yaw) in degrees for the quaternion's rotation."""
    r = quaternion_to_matrix(qx, qy, qz, qw)
    yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    pitch = math.degrees(math.asin(float(np.clip(-r[2, 0], -1.0, 1.0))))
    roll = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return (roll, pitch, yaw)


def _quat_of(pose: Pose) -> tuple[float, float, float, float]:
    if pose.has_quaternion:
        assert pose.qx is not None and pose.qy is not None and pose.qz is not None and pose.qw is not None
        return (pose.qx, pose.qy, pose.qz, pose.qw)
    return euler_to_quaternion_deg(pose.roll, pose.pitch, pose.yaw)


def rotation_matrix(pose: Pose) -> np.ndarray:
    """Body-to-map rotation built from the pose quaternion."""
    qx, qy, qz, qw = _quat_of(pose)
    return quaternion_to_matrix(qx, qy, qz, qw)


def heading_deg(pose: Pose) -> float:
    """Yaw normalised to [0, 360), via the sign-invariant quaternion formula."""
    if pose.has_quaternion:
        assert pose.qx is not None and pose.qy is not None and pose.qz is not None and pose.qw is not None
        qx, qy, qz, qw = normalize_quaternion(pose.qx, pose.qy, pose.qz, pose.qw)
        return float(math.degrees(math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))) % 360.0)
    return float(pose.yaw % 360.0)


def heading_vector(pose: Pose) -> np.ndarray:
    """Unit vector of the robot's forward axis projected onto the map plane."""
    fwd = rotation_matrix(pose) @ np.array([1.0, 0.0, 0.0])
    xy = fwd[:2]
    return xy / np.linalg.norm(xy)


def tilt_deg(pose: Pose) -> float:
    """Angle between the body z-axis and map z-axis, in degrees."""
    cos_tilt = np.clip(rotation_matrix(pose)[2, 2], -1.0, 1.0)
    return float(np.degrees(np.arccos(cos_tilt)))
