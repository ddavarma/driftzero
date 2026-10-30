import pytest

from fleet_telemetry.client import TelemetryParseError, parse_batch, parse_frame


def test_parse_frame_reads_pose_and_noise(fleet_payload):
    frame = parse_frame(fleet_payload["frames"][0])
    assert frame.robot_id == "amr-01"
    assert (frame.pose.x, frame.pose.y, frame.pose.z) == (12.4, 3.1, 0.0)
    assert frame.pose.yaw == 87.0
    assert frame.noise == 0.04
    assert frame.battery == 0.82


def test_parse_batch_rejects_other_api_version(fleet_payload):
    fleet_payload["api_version"] = "v0"
    with pytest.raises(TelemetryParseError, match="api_version"):
        parse_batch(fleet_payload)


def test_missing_field_raises_parse_error(fleet_payload):
    raw = dict(fleet_payload["frames"][0])
    del raw["orientation"]
    with pytest.raises(TelemetryParseError, match="orientation"):
        parse_frame(raw)


def test_client_hits_v1_paths(client):
    frame = client.latest("amr-03")
    snapshot = client.fleet_snapshot()
    assert frame.robot_id == "amr-03"
    assert len(snapshot) == 4
    assert client.calls == ["/v1/robots/amr-03/telemetry/latest", "/v1/fleet/telemetry"]
