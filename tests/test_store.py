"""Tests for the production store (SQLite via SQLModel)."""

from audiobook_visualizer.models import Character, Frame, Scene
from audiobook_visualizer.store import ProductionStore


def _store(tmp_path):
    store = ProductionStore.open(tmp_path)
    pid = store.get_or_create_production(title="Moby", author="HM", style="cinematic")
    return store, pid


def test_production_is_singleton_and_backfills(tmp_path):
    store = ProductionStore.open(tmp_path)
    a = store.get_or_create_production()  # empty meta
    b = store.get_or_create_production(title="Moby", author="HM")
    assert a == b  # one production per db
    view = store.production_view(a)
    assert view.title == "Moby" and view.author == "HM"  # backfilled


def test_character_merge_unions_aliases_and_prefers_richer(tmp_path):
    store, pid = _store(tmp_path)
    store.upsert_characters(pid, [Character(name="Ahab", aliases=["Captain"], description="scarred")])
    store.upsert_characters(pid, [Character(name="ahab", aliases=["Skipper"], description="a tall scarred whaling captain")])
    chars = store.list_characters(pid)
    assert len(chars) == 1
    assert chars[0].description == "a tall scarred whaling captain"
    assert set(chars[0].aliases) == {"Captain", "Skipper"}


def test_synthetic_scene_carries_one_shot_and_links_characters(tmp_path):
    store, pid = _store(tmp_path)
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.upsert_characters(pid, [Character(name="Ahab", aliases=["Captain"], description="x")])
    store.persist_scenes_as_shots(pid, sid, [
        Scene(id="s1", title="Deck", visual_description="Ahab paces",
              characters_present=["Captain"], start_time=10.0, end_time=20.0),
    ])
    view = store.segment_view(sid)
    assert len(view.shots) == 1
    shot = view.shots[0]
    assert shot.slug == "s1" and shot.camera_move == "static"
    # The "Captain" alias resolves to the canonical character name via the link.
    assert shot.characters_present == ["Ahab"]
    assert (shot.start_time, shot.end_time) == (10.0, 20.0)


def test_persist_scenes_replaces_prior_content(tmp_path):
    store, pid = _store(tmp_path)
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.persist_scenes_as_shots(pid, sid, [Scene(id="a", visual_description="x")])
    store.persist_scenes_as_shots(pid, sid, [Scene(id="b", visual_description="y")])
    slugs = [s.slug for s in store.segment_view(sid).shots]
    assert slugs == ["b"]  # replaced, not appended


def test_frame_upsert_and_cache_key(tmp_path):
    store, pid = _store(tmp_path)
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.persist_scenes_as_shots(pid, sid, [Scene(id="s1", visual_description="x")])
    store.upsert_frame(
        sid, Frame(scene_id="s1", prompt="p", image_path="/f/s1.png", provider="stub", model="m"),
        cache_key="K1",
    )
    assert store.frame_cache(sid, "s1") == ("/f/s1.png", "K1")
    # Upsert again -> single row updated, not duplicated.
    store.upsert_frame(
        sid, Frame(scene_id="s1", prompt="p2", image_path="/f/s1.png", provider="stub", model="m"),
        cache_key="K2",
    )
    assert store.frame_cache(sid, "s1") == ("/f/s1.png", "K2")
    f = store.get_frame(sid, "s1")
    assert f.prompt == "p2" and f.scene_id == "s1"


def test_segments_ordered_by_start(tmp_path):
    store, pid = _store(tmp_path)
    store.get_or_create_segment(pid, "0060-120min", 3600.0, 7200.0)
    store.get_or_create_segment(pid, "0000-60min", 0.0, 3600.0)
    labels = [lbl for _id, lbl, _s, _e in store.list_segments(pid)]
    assert labels == ["0000-60min", "0060-120min"]


def test_continuity_notes_roundtrip(tmp_path):
    store, pid = _store(tmp_path)
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.add_continuity_notes(sid, [
        {"severity": "warning", "category": "appearance", "message": "hat changed", "status": "open"},
        {"severity": "info", "category": "timeline", "message": "ok", "status": "resolved"},
    ])
    assert len(store.list_continuity_notes(sid)) == 2
    open_notes = store.list_continuity_notes(sid, status="open")
    assert len(open_notes) == 1 and open_notes[0].message == "hat changed"


def test_load_segment_analysis_roundtrips_render_units(tmp_path):
    store, pid = _store(tmp_path)
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.upsert_characters(pid, [Character(name="Ahab", description="x")])
    store.persist_scenes_as_shots(pid, sid, [
        Scene(id="s1", visual_description="harbor", start_time=0.0, end_time=10.0),
        Scene(id="s2", visual_description="deck", characters_present=["Ahab"], start_time=10.0, end_time=20.0),
    ])
    ba = store.load_segment_analysis(pid, sid)
    assert ba.title == "Moby"
    assert [s.id for s in ba.scenes] == ["s1", "s2"]
    assert ba.scenes[1].characters_present == ["Ahab"]
    assert len(ba.characters) == 1
