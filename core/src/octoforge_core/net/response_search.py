"""Literal occurrence scanning and response-window coalescing."""


def find_positions(target: str, pattern: str) -> list[tuple[int, int]]:
    """Return every case-insensitive literal occurrence as (start, end)."""
    hay = target.lower()
    needle = pattern.lower()
    positions: list[tuple[int, int]] = []
    start = hay.find(needle)
    while start != -1:
        positions.append((start, start + len(pattern)))
        start = hay.find(needle, start + 1)
    return positions


def merge_windows(windows: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Merge overlapping windows without duplicating dense matches."""
    merged: list[tuple[int, int, int]] = []
    for start, end, at in sorted(windows):
        if merged and start <= merged[-1][1]:
            old_start, old_end, old_at = merged[-1]
            merged[-1] = (old_start, max(old_end, end), old_at)
        else:
            merged.append((start, end, at))
    return merged
