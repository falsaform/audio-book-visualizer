"""The analysis orchestrator.

Given book text (and optional chapter structure), the ``Analyzer``:

1. Chunks the text to fit the model context.
2. Builds a character bible (extract per chunk, then merge/dedupe).
3. Extracts key visual moments (scenes) per chunk.

It returns a :class:`BookAnalysis`. Image prompt construction and audio
alignment live in their own modules so this stays focused on "text -> meaning".
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from ..config import AnalysisConfig
from ..models import BookAnalysis, Character, Scene
from ..utils.llm import ClaudeClient
from . import prompts

ProgressFn = Callable[[str], None]


class Analyzer:
    def __init__(self, config: AnalysisConfig, on_progress: Optional[ProgressFn] = None):
        self.config = config
        self._client = ClaudeClient(model=config.model, temperature=config.temperature)
        self._progress = on_progress or (lambda _msg: None)

    # -- public API ---------------------------------------------------------

    def analyze(
        self,
        chapters: list[tuple[str, str]],
        title: str = "",
        author: str = "",
    ) -> BookAnalysis:
        """Analyze a book given a list of ``(chapter_title, chapter_text)``."""
        chunks = self._chunk(chapters)
        self._progress(f"Analyzing {len(chunks)} text chunk(s)")

        characters = self._build_character_bible(chunks)
        self._progress(f"Identified {len(characters)} character(s)")

        scenes = self._extract_scenes(chunks, characters)
        self._progress(f"Extracted {len(scenes)} scene(s)")

        return BookAnalysis(
            title=title, author=author, characters=characters, scenes=scenes
        )

    # -- chunking -----------------------------------------------------------

    def _chunk(self, chapters: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Split chapters into model-sized chunks, preserving chapter labels."""
        limit = self.config.max_chars_per_chunk
        chunks: list[tuple[str, str]] = []
        for chap_title, text in chapters:
            text = text.strip()
            if not text:
                continue
            if len(text) <= limit:
                chunks.append((chap_title, text))
                continue
            # Split long chapters on paragraph boundaries.
            buffer = ""
            for para in text.split("\n\n"):
                if len(buffer) + len(para) + 2 > limit and buffer:
                    chunks.append((chap_title, buffer.strip()))
                    buffer = ""
                buffer += para + "\n\n"
            if buffer.strip():
                chunks.append((chap_title, buffer.strip()))
        return chunks

    # -- characters ---------------------------------------------------------

    def _build_character_bible(self, chunks: list[tuple[str, str]]) -> list[Character]:
        partial_lists: list[list[dict]] = []
        for i, (_title, text) in enumerate(chunks, 1):
            self._progress(f"Extracting characters from chunk {i}/{len(chunks)}")
            try:
                data = self._client.complete_json(
                    prompts.CHARACTER_SYSTEM,
                    prompts.CHARACTER_PROMPT.format(text=text),
                )
                if isinstance(data, list):
                    partial_lists.append(data)
            except Exception as exc:  # noqa: BLE001 - keep going on one bad chunk
                self._progress(f"  (character extraction failed on chunk {i}: {exc})")

        if not partial_lists:
            return []
        if len(partial_lists) == 1:
            return _to_characters(partial_lists[0])

        # Merge per-chunk lists into one deduplicated bible.
        self._progress("Merging character lists")
        try:
            merged = self._client.complete_json(
                prompts.CHARACTER_MERGE_SYSTEM,
                prompts.CHARACTER_MERGE_PROMPT.format(
                    lists=json.dumps(partial_lists, ensure_ascii=False)
                ),
            )
            if isinstance(merged, list):
                return _to_characters(merged)
        except Exception as exc:  # noqa: BLE001
            self._progress(f"  (merge failed, falling back to naive dedupe: {exc})")
        return _naive_merge(partial_lists)

    # -- scenes -------------------------------------------------------------

    def _extract_scenes(
        self, chunks: list[tuple[str, str]], characters: list[Character]
    ) -> list[Scene]:
        char_summary = _character_summary(characters)
        scenes: list[Scene] = []
        for i, (chap_title, text) in enumerate(chunks, 1):
            self._progress(f"Extracting scenes from chunk {i}/{len(chunks)}")
            chapter_note = f" (from '{chap_title}')" if chap_title else ""
            try:
                data = self._client.complete_json(
                    prompts.SCENE_SYSTEM,
                    prompts.SCENE_PROMPT.format(
                        n=self.config.scenes_per_chunk,
                        characters=char_summary or "(none identified yet)",
                        chapter_note=chapter_note,
                        text=text,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._progress(f"  (scene extraction failed on chunk {i}: {exc})")
                continue
            if not isinstance(data, list):
                continue
            for j, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                scenes.append(
                    Scene(
                        id=f"scene_{i:03d}_{j:02d}",
                        chapter=chap_title or None,
                        title=str(item.get("title", "")).strip(),
                        summary=str(item.get("summary", "")).strip(),
                        setting=str(item.get("setting", "")).strip(),
                        time_of_day=str(item.get("time_of_day", "")).strip(),
                        mood=str(item.get("mood", "")).strip(),
                        characters_present=[
                            str(c).strip()
                            for c in item.get("characters_present", [])
                            if str(c).strip()
                        ],
                        visual_description=str(item.get("visual_description", "")).strip(),
                        source_excerpt=str(item.get("source_excerpt", "")).strip(),
                    )
                )
        return scenes


# -- helpers ----------------------------------------------------------------


def _to_characters(items: list[dict]) -> list[Character]:
    out: list[Character] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append(
            Character(
                name=str(item["name"]).strip(),
                aliases=[str(a).strip() for a in item.get("aliases", []) if str(a).strip()],
                role=str(item.get("role", "")).strip(),
                description=str(item.get("description", "")).strip(),
            )
        )
    return out


def _naive_merge(partial_lists: list[list[dict]]) -> list[Character]:
    """Fallback dedupe by lowercased name when the model merge fails."""
    by_name: dict[str, Character] = {}
    for items in partial_lists:
        for char in _to_characters(items):
            key = char.name.lower()
            if key not in by_name:
                by_name[key] = char
            elif len(char.description) > len(by_name[key].description):
                by_name[key].description = char.description
    return list(by_name.values())


def _character_summary(characters: list[Character], limit: int = 40) -> str:
    lines = []
    for char in characters[:limit]:
        alias = f" (aka {', '.join(char.aliases)})" if char.aliases else ""
        lines.append(f"- {char.name}{alias}: {char.description}")
    return "\n".join(lines)
