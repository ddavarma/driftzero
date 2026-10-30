"""Domain types for robot telemetry, mirroring the v2 wire format."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pose:
    """Robot pose in the fleet map frame.

    Position is in metres. Orientation is carried by the v2 unit
    quaternion (``qx, qy, qz, qw``; Hamilton convention, scalar last,
    sign not canonicalised). ``roll``/``pitch``/``yaw`` remain as
    derived compatibility values in degrees (intrinsic Z-Y-X order),
    computed from the quaternion at parse time.
    """

    x: float
    y: float
    z: float
    roll: float
    pitch: float
    yaw: float
    qx: float | None = None
    qy: float | None = None
    qz: float | None = None
    qw: float | None = None

    @property
    def has_quaternion(self) -> bool:
        return self.qx is not None and self.qy is not None and self.qz is not None and self.qw is not None


Covariance = tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class TelemetryFrame:
    robot_id: str
    timestamp: float  # unix seconds
    pose: Pose
    noise: float  # scalar 1-sigma position uncertainty, metres: sqrt(trace(P_pos) / 3)
    battery: float  # state of charge, 0..1
    covariance: Covariance | None = None  # 6x6 symmetric pose covariance, row-major
    health: float = 1.0  # composite health score, 0..1
