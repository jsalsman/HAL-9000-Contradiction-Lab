"""Wilson intervals and Cohen's kappa."""

import pytest

from hal.stats import cohen_kappa, wilson_interval


def test_wilson_known_values():
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-4)
    assert high == pytest.approx(0.7634, abs=1e-4)


def test_wilson_edges_stay_in_unit_interval():
    low, high = wilson_interval(0, 5)
    assert low == 0.0 and high == pytest.approx(0.4345, abs=1e-4)
    low, high = wilson_interval(5, 5)
    assert high == 1.0 and low == pytest.approx(0.5655, abs=1e-4)


def test_wilson_empty_and_invalid():
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_wilson_narrows_with_n():
    small = wilson_interval(3, 10)
    large = wilson_interval(30, 100)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_kappa():
    a = ["A", "A", "B", "B"]
    assert cohen_kappa(a, a) == 1.0
    assert cohen_kappa(a, ["B", "B", "A", "A"]) == pytest.approx(-1.0)
    # Classic example: po=0.7, pe=0.5 -> 0.4.
    x = ["y"] * 5 + ["n"] * 5
    y = ["y"] * 4 + ["n"] + ["y"] * 2 + ["n"] * 3
    assert cohen_kappa(x, y) == pytest.approx(0.4)
    assert cohen_kappa([], []) is None
    assert cohen_kappa(["A", "A"], ["A", "A"]) == 1.0
    with pytest.raises(ValueError):
        cohen_kappa(["A"], [])
