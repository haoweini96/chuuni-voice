"""Tests for bubble_sort."""

from final_test import bubble_sort


def test_sorts_basic_list():
    assert bubble_sort([3, 1, 4, 1, 5, 9, 2, 6]) == [1, 1, 2, 3, 4, 5, 6, 9]


def test_already_sorted():
    assert bubble_sort([1, 2, 3]) == [1, 2, 3]


def test_reverse_sorted():
    assert bubble_sort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]


def test_single_element():
    assert bubble_sort([42]) == [42]


def test_empty_list():
    assert bubble_sort([]) == []


def test_does_not_mutate_input():
    original = [3, 1, 2]
    bubble_sort(original)
    assert original == [3, 1, 2]
