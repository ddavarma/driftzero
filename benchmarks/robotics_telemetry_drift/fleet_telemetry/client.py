"""Parse fleet telemetry API v2 payloads into domain objects."""
from __future__ import annotations

import math
from typing import Any, Callable

from fleet_telemetry.models import Pose, TelemetryFrame
from fleet_telemetry.orientation import normalize_quaternion, quaternion_to_euler_deg

API_VERSION = "v2"
BASE_PATH = "/v2"

Transport = Callable[[str], dict[str, Any]]


class TelemetryParseError(ValueError):
    """Raised when a payload does not match the v2 schema."""


def _parse_covariance(raw: Any) -> tuple[tuple[float, ...], ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 6:
        raise TelemetryParseError(f"covariance must be 6x6, got {raw!r}")
    rows: list[tuple[float, ...]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != 6:
            raise TelemetryParseError(f"covariance must be 6x6, got {raw!r}")
        try:
            values = tuple(float(v) for v in row)
        except (TypeError, ValueError) as exc:
            raise TelemetryParseError(f"covariance must hold numbers, got {row!r}") from exc
        if not all(math.isfinite(v) for v in values):
            raise TelemetryParseError(f"covariance must hold finite numbers, got {row!r}")
        rows.append(values)
    return tuple(rows)


def parse_frame(payload: dict[str, Any]) -> TelemetryFrame:
    try:
        pos = payload["position"]
        ori = payload["orientation"]
        qx = float(ori["qx"])
        qy = float(ori["qy"])
        qz = float(ori["qz"])
        qw = float(ori["qw"])
        try:
            qx, qy, qz, qw = normalize_quaternion(qx, qy, qz, qw)
        except ValueError as exc:
            raise TelemetryParseError(f"invalid orientation quaternion: {exc}") from exc
        # Compatibility Euler angles, quoted to 1e-3 deg (v1 wire resolution).
        roll, pitch, yaw = (round(v, 3) for v in quaternion_to_euler_deg(qx, qy, qz, qw))
        pose = Pose(
            x=float(pos["x"]),
            y=float(pos["y"]),
            z=float(pos["z"]),
            roll=roll,
            pitch=pitch,
            yaw=yaw,
            qx=qx,
            qy=qy,
            qz=qz,
            qw=qw,
        )
        covariance = _parse_covariance(payload["covariance"])
        sigma = math.sqrt(sum(covariance[i][i] for i in range(3)) / 3.0)
        if not math.isfinite(sigma) or sigma <= 0:
            raise TelemetryParseError(f"position sigma must be positive, got {sigma}")
        health = float(payload["health"])
        if not math.isfinite(health) or not 0.0 <= health <= 1.0:
            raise TelemetryParseError(f"health must be within 0..1, got {health}")
        return TelemetryFrame(
            robot_id=str(payload["robot_id"]),
            timestamp=float(payload["timestamp"]),
            pose=pose,
            noise=sigma,
            battery=float(payload["battery"]),
            covariance=covariance,
            health=health,
        )
    except KeyError as exc:
        raise TelemetryParseError(f"missing field {exc.args[0]!r}") from exc


def parse_batch(payload: dict[str, Any]) -> list[TelemetryFrame]:
    version = payload.get("api_version")
    if version != API_VERSION:
        raise TelemetryParseError(f"unsupported api_version {version!r}, expected {API_VERSION!r}")
    return [parse_frame(f) for f in payload["frames"]]


class FleetClient:
    """Thin client over an injected transport (HTTP in prod, fixtures in tests)."""

    def __init__(self, transport: Transport, base_path: str = BASE_PATH):
        self._transport = transport
        self._base = base_path.rstrip("/")

    def latest(self, robot_id: str) -> TelemetryFrame:
        return parse_frame(self._transport(f"{self._base}/robots/{robot_id}/telemetry/latest"))

    def fleet_snapshot(self) -> list[TelemetryFrame]:
        return parse_batch(self._transport(f"{self._base}/fleet/telemetry"))
