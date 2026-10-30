import numpy as np
import pytest

from fleet_telemetry.fusion import fuse_heading, fuse_positions


def test_fused_position_favours_low_noise(frames):
    precise, noisy = frames["amr-01"], frames["amr-03"]
    fused, sigma = fuse_positions([precise, noisy])
    d_precise = np.linalg.norm(fused - [precise.pose.x, precise.pose.y, precise.pose.z])
    d_noisy = np.linalg.norm(fused - [noisy.pose.x, noisy.pose.y, noisy.pose.z])
    assert d_precise < d_noisy
    assert sigma < precise.noise


def test_equal_noise_gives_plain_mean(frames):
    a, b = frames["amr-01"], frames["amr-01"]
    fused, sigma = fuse_positions([a, b])
    assert np.allclose(fused, [a.pose.x, a.pose.y, a.pose.z])
    assert sigma == pytest.approx(a.noise / np.sqrt(2))


def test_heading_mean_handles_wraparound(frames):
    # amr-03 sits at 359 deg; averaging with itself must stay near north, not 179.5
    assert fuse_heading([frames["amr-03"], frames["amr-03"]]) == pytest.approx(359.0)
    hdg = fuse_heading([frames["amr-01"], frames["amr-03"]])
    assert 0.0 <= hdg < 360.0
