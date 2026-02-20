"""Bubble sort implementation."""


def bubble_sort(arr: list) -> list:
    """Sort *arr* in ascending order using bubble sort. Returns a new list."""
    result = list(arr)
    n = len(result)
    for i in range(n):
        for j in range(n - i - 1):
            if result[j] > result[j + 1]:
                result[j], result[j + 1] = result[j + 1], result[j]
    return result
