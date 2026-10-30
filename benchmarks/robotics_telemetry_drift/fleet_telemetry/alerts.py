"""Per-frame safety and data-quality alerts."""
from __future__ import annotations

from dataclasses import dataclass

from fleet_telemetry.models import TelemetryFrame
from fleet_telemetry.orientation import tilt_deg

MAX_TILT_DEG = 15.0
MAX_NOISE_M = 0.25
MIN_BATTERY = 0.15
MIN_HEALTH = 0.5


@dataclass(frozen=True)
class Alert:
    robot_id: str
    kind: str  # "tilt" | "noisy" | "battery" | "health"
    message: str


def check_frame(
    frame: TelemetryFrame,
    max_tilt_deg: float = MAX_TILT_DEG,
    max_noise_m: float = MAX_NOISE_M,
    min_battery: float = MIN_BATTERY,
    min_health: float = MIN_HEALTH,
) -> list[Alert]:
    alerts: list[Alert] = []
    tilt = tilt_deg(frame.pose)
    if tilt > max_tilt_deg:
        alerts.append(Alert(frame.robot_id, "tilt", f"tilt {tilt:.1f} deg exceeds {max_tilt_deg:.1f}"))
    if frame.noise > max_noise_m:
        alerts.append(Alert(frame.robot_id, "noisy", f"position noise {frame.noise:.2f} m exceeds {max_noise_m:.2f}"))
    if frame.battery < min_battery:
        alerts.append(Alert(frame.robot_id, "battery", f"battery {frame.battery:.0%} below {min_battery:.0%}"))
    if frame.health < min_health:
        alerts.append(Alert(frame.robot_id, "health", f"health {frame.health:.2f} below {min_health:.2f}"))
    return alerts
