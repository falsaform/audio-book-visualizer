"""Gallery rendering, including the director-mode storyboard grouping."""

from audiobook_visualizer.crew import SceneDraft, ShotDraft
from audiobook_visualizer.gallery import render_gallery
from audiobook_visualizer.models import Character
from audiobook_visualizer.store import ProductionStore


def test_storyboard_groups_shots_under_sluglines(tmp_path):
    store = ProductionStore.open(tmp_path)
    pid = store.get_or_create_production(title="Moby", author="HM")
    sid = store.get_or_create_segment(pid, "full", 0.0, 100.0)
    store.upsert_characters(pid, [Character(name="Ahab", description="captain")])
    store.persist_screenplay(pid, sid, [
        SceneDraft(heading="EXT. HARBOR - DAWN", title="Arrival", start_time=0.0, end_time=50.0,
                   shots=[ShotDraft(shot_type="wide establishing shot", camera_move="push_in",
                                    visual_description="grey harbor")]),
        SceneDraft(heading="INT. SHIP - NIGHT", title="Ahab", start_time=50.0, end_time=100.0,
                   shots=[ShotDraft(shot_type="close-up", camera_move="tilt_up",
                                    visual_description="the captain")]),
    ])

    out = render_gallery(store.production_view(pid), tmp_path)
    html = out.read_text()
    # Scene sluglines become section headers.
    assert '<h2 class="section">EXT. HARBOR - DAWN</h2>' in html
    assert '<h2 class="section">INT. SHIP - NIGHT</h2>' in html
    # Shots are annotated with their type + camera move.
    assert "push_in" in html and "tilt_up" in html
    assert "wide establishing shot" in html


def test_gallery_without_headings_has_no_sections(tmp_path, store_seeder):
    # Legacy/synthetic shots carry no slug line -> a flat gallery (no sections).
    store_seeder(tmp_path, [{"label": "full", "start": 0.0, "end": 10.0,
                             "scenes": [{"slug": "s1", "title": "A", "desc": "x"}]}])
    store = ProductionStore.open(tmp_path)
    out = render_gallery(store.production_view(store.get_production_id()), tmp_path)
    assert 'class="section"' not in out.read_text()
