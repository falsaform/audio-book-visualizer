"""Deterministic shot-timing allocation."""

from audiobook_visualizer.crew.timing import allocate_shot_timings, target_shot_count


def _tiles(spans, start, end):
    """Spans are contiguous, start at ``start`` and end exactly at ``end``."""
    if not spans:
        return False
    if spans[0][0] != start or spans[-1][1] != end:
        return False
    return all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


def test_tiles_window_exactly():
    spans = allocate_shot_timings(0.0, 100.0, [1, 1, 1])
    assert _tiles(spans, 0.0, 100.0)
    assert len(spans) == 3


def test_weights_distribute_remainder():
    spans = allocate_shot_timings(0.0, 90.0, [2, 1])  # no min -> 60/30
    durs = [e - s for s, e in spans]
    assert durs == [60.0, 30.0]
    assert _tiles(spans, 0.0, 90.0)


def test_min_shot_respected_when_window_allows():
    spans = allocate_shot_timings(0.0, 100.0, [10, 0, 0], min_shot=20.0)
    durs = [e - s for s, e in spans]
    # Each shot gets at least the 20s floor; the weighted one takes the rest.
    assert durs[1] == 20.0 and durs[2] == 20.0
    assert durs[0] == 60.0
    assert _tiles(spans, 0.0, 100.0)


def test_min_shot_scaled_down_when_window_too_short():
    spans = allocate_shot_timings(0.0, 6.0, [1, 1, 1], min_shot=5.0)
    # 3 * 5s can't fit in 6s -> min scales to 2s each; still tiles exactly.
    assert _tiles(spans, 0.0, 6.0)
    assert all(abs((e - s) - 2.0) < 1e-9 for s, e in spans)


def test_last_shot_pinned_to_end():
    spans = allocate_shot_timings(10.0, 33.0, [1, 1, 1, 1])
    assert spans[-1][1] == 33.0
    assert _tiles(spans, 10.0, 33.0)


def test_single_and_empty():
    assert allocate_shot_timings(5.0, 9.0, [3]) == [(5.0, 9.0)]
    assert allocate_shot_timings(0.0, 10.0, []) == []


def test_zero_or_inverted_window():
    spans = allocate_shot_timings(50.0, 50.0, [1, 1])
    assert spans == [(50.0, 50.0), (50.0, 50.0)]


def test_zero_weights_fall_back_to_min_floor():
    spans = allocate_shot_timings(0.0, 30.0, [0, 0, 0], min_shot=5.0)
    # All-zero weights -> each gets the floor, remainder unallocated stays with
    # earlier shots via the running cursor; still tiles and last hits the end.
    assert _tiles(spans, 0.0, 30.0)


def test_target_shot_count():
    assert target_shot_count(60.0, 8.0, max_shots=6) == 6  # 7.5 -> clamp to 6
    assert target_shot_count(20.0, 8.0, max_shots=6) == 2  # round(2.5) -> 2 (banker's)
    assert target_shot_count(28.0, 8.0, max_shots=6) == 4  # round(3.5) -> 4
    assert target_shot_count(0.0, 8.0, max_shots=6) == 1
    assert target_shot_count(100.0, 0.0, max_shots=6) == 1
