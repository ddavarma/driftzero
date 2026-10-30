import pytest

from driftzero.telemetry import RATES, cost_from_usage, usd


def test_rates_table():
    assert RATES["muse-spark-1.3"] == (1.25, 0.15, 4.25)
    assert RATES["muse-spark-1.3-contributor"] == (0.10, 0.002, 0.20)
    assert RATES["muse-wonder-1.0"] == (4.00, 0.20, 20.00)


def test_cached_tokens_are_a_subset_of_input():
    # 1M input of which 0.9M cached, 0.1M output
    assert usd("muse-spark-1.3", 1_000_000, 900_000, 100_000) == pytest.approx(0.125 + 0.135 + 0.425)
    assert usd("muse-spark-1.3", 1_000_000, 900_000, 100_000, contributor=True) == pytest.approx(0.01 + 0.0018 + 0.02)
    assert usd("unknown-model", 1, 0, 1) is None


def test_cost_of_the_stage4_live_run():
    usage = {"models": [{"model": "muse-spark-1.3", "input_tokens": 1393249, "cached_tokens": 1279572,
                         "output_tokens": 30026, "reasoning_tokens": 19186, "calls": 22}]}
    c = cost_from_usage(usage)
    assert c.usd == pytest.approx(0.4616, abs=1e-4)
    assert c.cached_pct == pytest.approx(91.8, abs=0.1)
    assert cost_from_usage(usage, free=True).usd == 0.0
    assert (c + c).input_tokens == 2 * 1393249
