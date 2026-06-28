"""Crew orchestration (script writer -> director) with a mocked agent runner."""

from audiobook_visualizer.config import Config
from audiobook_visualizer.crew import Crew
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Character, Paragraph


class FakeRunner:
    """Dispatches on the role's system prompt to return canned writer/director JSON."""

    def json(self, role, system, prompt, max_tokens=4096):
        if "screenwriter" in system.lower():
            return [
                {"start_paragraph": 0, "heading": "EXT. HARBOR - DAWN", "title": "Arrival",
                 "synopsis": "x", "action": "y", "setting": "harbor", "time_of_day": "dawn",
                 "mood": "cold", "characters_present": []},
                {"start_paragraph": 2, "heading": "INT. SHIP - NIGHT", "title": "Ahab broods",
                 "synopsis": "x", "action": "y", "setting": "ship", "time_of_day": "night",
                 "mood": "ominous", "characters_present": ["Ahab"]},
            ]
        return [
            {"shot_type": "wide", "camera_move": "push in", "subject": "harbor",
             "visual_description": "grey harbor", "characters_present": [], "duration_weight": 2},
            {"shot_type": "close-up", "camera_move": "NONSENSE", "subject": "face",
             "visual_description": "weathered face", "characters_present": ["Ahab"],
             "duration_weight": 1},
        ]


def _structure() -> AudiobookStructure:
    return AudiobookStructure(title="Moby", chapters=[
        AudioChapter(index=0, title="Chapter One", start=0.0, end=120.0, paragraphs=[
            Paragraph(index=0, text="Call me Ishmael.", start=0.0, end=20.0),
            Paragraph(index=1, text="The harbor lay grey.", start=20.0, end=40.0),
            Paragraph(index=2, text="Ahab paced the deck.", start=40.0, end=80.0),
            Paragraph(index=3, text="He stared at the sea.", start=80.0, end=120.0),
        ]),
    ])


def _crew() -> Crew:
    cfg = Config()
    cfg.analysis.mode = "director"
    return Crew(cfg, FakeRunner())


def test_writer_scenes_tile_the_chapter():
    scenes = _crew().build(_structure(), [Character(name="Ahab", description="captain")])
    assert [s.title for s in scenes] == ["Arrival", "Ahab broods"]
    # Scene 1 covers paras 0-1 (0-40s), scene 2 covers paras 2-3 (40-120s): contiguous.
    assert (scenes[0].start_time, scenes[0].end_time) == (0.0, 40.0)
    assert (scenes[1].start_time, scenes[1].end_time) == (40.0, 120.0)


def test_shots_tile_each_scene_window_with_weights():
    scenes = _crew().build(_structure(), [])
    s0 = scenes[0]
    assert [round(sh.start_time, 1) for sh in s0.shots] == [0.0, 25.7]  # weight 2:1 over 0-40
    assert s0.shots[0].start_time == s0.start_time
    assert s0.shots[-1].end_time == s0.end_time  # last pinned to the scene end


def test_unknown_camera_move_coerced_to_static():
    scenes = _crew().build(_structure(), [])
    moves = {sh.camera_move for s in scenes for sh in s.shots}
    assert "push_in" in moves          # "push in" normalised
    assert "static" in moves           # "NONSENSE" coerced
    assert "NONSENSE" not in moves


def test_max_shots_per_scene_is_capped():
    cfg = Config()
    cfg.analysis.mode = "director"
    cfg.crew.timing.max_shots_per_scene = 1

    class ManyShots(FakeRunner):
        def json(self, role, system, prompt, max_tokens=4096):
            data = super().json(role, system, prompt, max_tokens)
            return data if "screenwriter" in system.lower() else data * 5  # 10 shots

    scenes = Crew(cfg, ManyShots()).build(_structure(), [])
    assert all(len(s.shots) == 1 for s in scenes)


def test_writer_failure_falls_back_to_whole_chapter():
    class Boom(FakeRunner):
        def json(self, role, system, prompt, max_tokens=4096):
            if "screenwriter" in system.lower():
                raise RuntimeError("writer down")
            return super().json(role, system, prompt, max_tokens)

    scenes = Crew(Config(), Boom()).build(_structure(), [])
    assert len(scenes) == 1                       # one whole-chapter scene
    assert scenes[0].start_time == 0.0 and scenes[0].end_time == 120.0
    assert scenes[0].shots                         # director still ran
