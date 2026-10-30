import json
from pathlib import Path

import pytest

from fleet_telemetry.client import FleetClient

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fleet_payload():
    return json.loads((FIXTURES / "fleet_v1.json").read_text())


@pytest.fixture
def frames(fleet_payload):
    from fleet_telemetry.client import parse_batch

    return {f.robot_id: f for f in parse_batch(fleet_payload)}


@pytest.fixture
def client(fleet_payload):
    by_id = {f["robot_id"]: f for f in fleet_payload["frames"]}
    calls = []

    def transport(path):
        calls.append(path)
        if path.endswith("/fleet/telemetry"):
            return fleet_payload
        robot_id = path.split("/")[-3]
        return by_id[robot_id]

    c = FleetClient(transport)
    c.calls = calls
    return c
