"""Charging-dock scheduling from battery state of charge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

CHARGE_RATE_PER_MIN = 0.012  # fraction of capacity per minute on a standard dock
TARGET_CHARGE = 0.95
DEFAULT_THRESHOLD = 0.30


@dataclass(frozen=True)
class DockSlot:
    robot_id: str
    dock: int
    start_min: float
    minutes: float


def charge_minutes(battery: float, target: float = TARGET_CHARGE, rate: float = CHARGE_RATE_PER_MIN) -> float:
    return max(0.0, target - battery) / rate


def plan_charging(levels: Mapping[str, float], docks: int, threshold: float = DEFAULT_THRESHOLD) -> list[DockSlot]:
    """Send robots below `threshold` to `docks` chargers, emptiest first, on the earliest-free dock."""
    if docks < 1:
        raise ValueError("need at least one dock")
    queue = sorted((battery, rid) for rid, battery in levels.items() if battery < threshold)
    free_at = [0.0] * docks
    slots: list[DockSlot] = []
    for battery, rid in queue:
        dock = min(range(docks), key=free_at.__getitem__)
        minutes = charge_minutes(battery)
        slots.append(DockSlot(rid, dock, free_at[dock], minutes))
        free_at[dock] += minutes
    return slots
