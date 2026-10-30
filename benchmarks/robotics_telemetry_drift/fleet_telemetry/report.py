"""Fleet-level summary for the ops dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from fleet_telemetry.alerts import Alert, check_frame
from fleet_telemetry.models import TelemetryFrame
from fleet_telemetry.orientation import heading_deg


@dataclass
class FleetSummary:
    n_robots: int
    mean_battery: float
    headings: dict[str, float]
    alerts: list[Alert] = field(default_factory=list)

    @property
    def healthy_ids(self) -> list[str]:
        flagged = {a.robot_id for a in self.alerts}
        return sorted(rid for rid in self.headings if rid not in flagged)


def summarize(frames: Sequence[TelemetryFrame]) -> FleetSummary:
    alerts = [a for f in frames for a in check_frame(f)]
    return FleetSummary(
        n_robots=len(frames),
        mean_battery=sum(f.battery for f in frames) / len(frames) if frames else 0.0,
        headings={f.robot_id: heading_deg(f.pose) for f in frames},
        alerts=alerts,
    )


def format_summary(summary: FleetSummary) -> str:
    lines = [f"robots: {summary.n_robots}  mean battery: {summary.mean_battery:.0%}"]
    for rid, hdg in sorted(summary.headings.items()):
        lines.append(f"  {rid}  heading {hdg:6.1f} deg")
    for a in summary.alerts:
        lines.append(f"  ALERT [{a.kind}] {a.robot_id}: {a.message}")
    return "\n".join(lines)
