"""Domain types for robot telemetry, mirroring the v1 wire format."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pose:
    """Robot pose in the fleet map frame.

    Position is in metres. Orientation is Euler angles in **degrees**,
    intrinsic Z-Y-X order (yaw, then pitch, then roll), as served by v1.
    """

    x: float
    y: float
    z: float
    roll: float
    pitch: float
    yaw: float


@dataclass(frozen=True)
class TelemetryFrame:
    robot_id: str
    timestamp: float  # unix seconds
    pose: Pose
    noise: float  # scalar 1-sigma position noise, metres
    battery: float  # state of charge, 0..1
