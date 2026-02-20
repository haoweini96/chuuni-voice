import pytest
from hero_stats import calculate_power


def test_basic_power_level1():
    # (10*2 + 8*1.5 + 12) * 1.0 = 44
    assert calculate_power(10, 8, 12, 1) == 44


def test_power_scales_with_level():
    power_lv1 = calculate_power(30, 25, 20, 1)
    power_lv3 = calculate_power(30, 25, 20, 3)
    assert power_lv3 > power_lv1


def test_legend_hero():
    # (80*2 + 60*1.5 + 70) * 3.0 = (160+90+70)*3 = 320*3 = 960
    assert calculate_power(80, 60, 70, 5) == 960


def test_invalid_level_raises():
    with pytest.raises(ValueError):
        calculate_power(10, 10, 10, 99)


def test_zero_stats():
    assert calculate_power(0, 0, 0, 1) == 0
