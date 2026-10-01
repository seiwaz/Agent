from __future__ import annotations

from decimal import Decimal as D

import pytest

from sp2l.core.numeric import is_on_tick, median_even


def test_median_even_n20_is_mean_of_10th_and_11th():
    values = [D(v) for v in range(20, 0, -1)]  # 20..1 unsorted order
    assert median_even(values) == D("10.5")


def test_median_even_rejects_odd():
    with pytest.raises(ValueError):
        median_even([D(1), D(2), D(3)])


def test_is_on_tick():
    assert is_on_tick(D("100.10"), D("0.1"))
    assert not is_on_tick(D("100.05"), D("0.1"))
