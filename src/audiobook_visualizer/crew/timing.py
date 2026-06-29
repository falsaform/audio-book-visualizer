"""Deterministic shot-timing allocation.

The director (an LLM) only *suggests* relative ``duration_weight`` per shot;
Python pins the exact start/end times so the shots **tile the scene's audio
window with no gaps or overlaps** and the last shot ends exactly on the window
end. This keeps the animatic in sync with the narration regardless of what the
model returns.
"""

from __future__ import annotations


def allocate_shot_timings(
    start: float, end: float, weights: list[float], min_shot: float = 0.0
) -> list[tuple[float, float]]:
    """Split ``[start, end]`` into ``len(weights)`` contiguous spans.

    Each span gets at least ``min_shot`` seconds when the window allows (the
    minimum is scaled down if the window is too short to give every shot that
    much), and the remainder is distributed in proportion to the weights. The
    spans tile the window exactly: span ``i`` starts where span ``i-1`` ended and
    the final span ends on ``end``.
    """
    n = len(weights)
    if n == 0:
        return []
    start, end = float(start), float(end)
    if end <= start:
        return [(start, start) for _ in range(n)]
    if n == 1:
        return [(start, end)]

    window = end - start
    eff_min = min(max(0.0, min_shot), window / n)
    remainder = window - eff_min * n
    total_w = sum(w for w in weights if w > 0) or 0.0

    durations: list[float] = []
    for w in weights:
        share = (w / total_w) if (total_w > 0 and w > 0) else 0.0
        durations.append(eff_min + remainder * share)

    spans: list[tuple[float, float]] = []
    t = start
    for i, dur in enumerate(durations):
        s = t
        e = end if i == n - 1 else min(end, s + dur)
        if e < s:
            e = s
        spans.append((s, e))
        t = e
    return spans


def target_shot_count(duration: float, target_seconds_per_shot: float, max_shots: int) -> int:
    """How many shots to ask the director for, given a scene's duration."""
    if duration <= 0 or target_seconds_per_shot <= 0:
        return 1
    n = round(duration / target_seconds_per_shot)
    return max(1, min(int(n), max(1, max_shots)))
