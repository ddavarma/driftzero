# fleet-telemetry (DriftZero benchmark)

A small consumer library for the fleet telemetry API **v1**: parses frames, does orientation math
(Euler → rotation matrix, heading, tilt), inverse-variance fusion on the scalar `noise` field, and
raises tilt / noise / battery alerts for the ops dashboard.

The upstream API has shipped **v2** (quaternions, 6×6 covariance, health score); see
[`CHANGELOG_v2.md`](CHANGELOG_v2.md), [`openapi_v1.json`](openapi_v1.json) and
[`openapi_v2.json`](openapi_v2.json). This repo is the input DriftZero has to migrate.

```bash
pip install -e '.[test]'
pytest -q          # v1 suite: green on main
```

| module | role | touched by v2 |
|---|---|---|
| `models.py` | `Pose` / `TelemetryFrame` dataclasses | yes: Euler fields, `noise` |
| `client.py` | payload parsing, `/v1` paths | yes |
| `orientation.py` | rotation matrix, heading, tilt | yes |
| `fusion.py` | inverse-variance position/heading fusion | yes: `noise` |
| `alerts.py` | tilt / noisy / battery alerts | yes: `noise`, new `health` |
| `report.py` | fleet summary | indirectly |
