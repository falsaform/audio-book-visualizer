"""Crew orchestration (script writer -> director) with a mocked agent runner."""

from audiobook_visualizer.config import Config
from audiobook_visualizer.crew import Crew
from audiobook_visualizer.models import AudioChapter, AudiobookStructure, Character, Paragraph


class FakeRunner:
    """Dispatches on the role's system prompt to return canned per-role JSON.

    Checks the specific roles before "screenwriter" — the critic's system prompt
    legitimately contains the word "screenwriter" ("a screenwriter's breakdown").
    """

    def json(self, role, system, prompt, max_tokens=4096):
        s = system.lower()
        if "script editor" in s:           # critic: accept by default
            return {"revise": False, "notes": []}
        if "continuity" in s:              # continuity: no notes by default
            return []
        if "screenwriter" in s:            # script writer
            return [
                {"start_paragraph": 0, "heading": "EXT. HARBOR - DAWN", "title": "Arrival",
                 "synopsis": "x", "action": "y", "setting": "harbor", "time_of_day": "dawn",
                 "mood": "cold", "characters_present": []},
                {"start_paragraph": 2, "heading": "INT. SHIP - NIGHT", "title": "Ahab broods",
                 "synopsis": "x", "action": "y", "setting": "ship", "time_of_day": "night",
                 "mood": "ominous", "characters_present": ["Ahab"]},
            ]
        return [                            # director
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


def _director_cfg() -> Config:
    cfg = Config()
    cfg.analysis.mode = "director"
    return cfg


def _crew() -> Crew:
    return Crew(_director_cfg(), FakeRunner())


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


def test_script_critic_revises_then_accepts():
    class CriticRunner:
        def __init__(self):
            self.critic_calls = 0

        def json(self, role, system, prompt, max_tokens=4096):
            s = system.lower()
            if "script editor" in s:
                self.critic_calls += 1
                return {"revise": self.critic_calls == 1,  # revise once, then accept
                        "notes": [{"scene": 0, "issue": "split it", "suggestion": "x",
                                   "severity": "major"}]}
            if "continuity" in s:
                return []
            if "screenwriter" in s:
                title = "Revised" if "EDITOR'S NOTES" in prompt else "Original"
                return [{"start_paragraph": 0, "heading": "H", "title": title, "synopsis": "",
                         "action": "", "setting": "", "time_of_day": "", "mood": "",
                         "characters_present": []}]
            return [{"shot_type": "wide", "camera_move": "static", "subject": "x",
                     "visual_description": "d", "characters_present": [], "duration_weight": 1}]

    runner = CriticRunner()
    scenes = Crew(_director_cfg(), runner).build(_structure(), [])
    assert runner.critic_calls == 2          # revise, then accept
    assert all(s.title == "Revised" for s in scenes)
    assert all(s.revision == 1 for s in scenes)


def test_critic_loop_bounded_by_max_revisions():
    cfg = _director_cfg()
    cfg.crew.max_revisions = 2

    class AlwaysRevise(FakeRunner):
        def __init__(self):
            self.critic_calls = 0

        def json(self, role, system, prompt, max_tokens=4096):
            s = system.lower()
            if "script editor" in s:
                self.critic_calls += 1
                return {"revise": True, "notes": []}
            if "continuity" in s:
                return []
            return super().json(role, system, prompt, max_tokens)

    runner = AlwaysRevise()
    scenes = Crew(cfg, runner).build(_structure(), [])
    assert runner.critic_calls == 2          # capped, doesn't loop forever
    assert all(s.revision == 2 for s in scenes)


def test_continuity_review_returns_notes():
    class ContRunner(FakeRunner):
        def json(self, role, system, prompt, max_tokens=4096):
            if "continuity" in system.lower():
                return [
                    {"severity": "warning", "category": "wardrobe",
                     "message": "the hat appears then vanishes", "proposed_fix": "keep the hat"},
                    {"message": ""},  # dropped: no message
                    {"severity": "nonsense", "category": "x", "message": "bad sev -> info"},
                ]
            return super().json(role, system, prompt, max_tokens)

    crew = Crew(_director_cfg(), ContRunner())
    scenes = crew.build(_structure(), [])
    notes = crew.review_continuity(scenes, [])
    assert len(notes) == 2
    assert notes[0]["severity"] == "warning" and notes[0]["category"] == "wardrobe"
    assert notes[0]["proposed_fix"] == "keep the hat"
    assert notes[1]["severity"] == "info"  # coerced


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
