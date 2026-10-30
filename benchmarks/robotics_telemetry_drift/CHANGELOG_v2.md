# Fleet Telemetry API — v2.0.0

Released 2026-10-28. **Breaking.** v1 endpoints are deprecated and will be shut off on 2026-12-01.

## Base path

- All endpoints move from `/v1/...` to `/v2/...` (`/v2/robots/{robot_id}/telemetry/latest`, `/v2/fleet/telemetry`).
- `FleetSnapshot.api_version` is now `"v2"`.

## Orientation: Euler degrees → unit quaternion

- **Removed** `orientation.roll`, `orientation.pitch`, `orientation.yaw` (degrees, intrinsic Z-Y-X).
- **Added** `orientation.qx`, `orientation.qy`, `orientation.qz`, `orientation.qw`: a unit quaternion,
  Hamilton convention, scalar **last**, describing the same body-to-map rotation
  `R = Rz(yaw)·Ry(pitch)·Rx(roll)` that v1 encoded.
- The server does not canonicalise sign: `q` and `-q` are the same rotation and `qw` may be negative.
- Rationale: Euler angles hit gimbal lock on the new ramp-climbing AMR-X units at pitch ≈ ±90°, and
  every consumer was re-implementing degree/radian conversion.

Migration: build rotations from the quaternion directly. If you still need heading, use
`yaw = atan2(2(qw·qz + qx·qy), 1 − 2(qy² + qz²))`; do not round-trip through Euler for tilt.

## Uncertainty: scalar `noise` → 6×6 `covariance`

- **Removed** `noise` (isotropic 1-sigma position uncertainty, metres).
- **Added** `covariance`: 6×6 symmetric pose covariance as nested row-major arrays.
  Variable order `x, y, z` (m) then rotation about `X, Y, Z` (rad).
  The position block `covariance[0:3][0:3]` is in m².
- Migration: anything that weighted by `1 / noise²` should weight by the inverse of the position
  covariance block. A scalar equivalent of the old `noise` is `sqrt(trace(P_pos) / 3)`.

## New: `health`

- **Added** required `health` (0..1): composite localisation/drive health from the on-robot monitor.
  1 = nominal; values below 0.5 mean the robot should be serviced.
- Ops dashboards should raise a health alert below 0.5.

## Unchanged

`robot_id`, `timestamp`, `position{x,y,z}`, `battery`, error codes, auth.
