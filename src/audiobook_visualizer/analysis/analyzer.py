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
from ..models import AudiobookStructure, BookAnalysis, Character, Scene
from ..utils.llm import build_llm_client
from . import prompts


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        s in msg
        for s in ("authentication", "x-api-key", "401", "unauthorized", "invalid api key")
    )

ProgressFn = Callable[[str], None]


class Analyzer:
    def __init__(self, config: AnalysisConfig, on_progress: Optional[ProgressFn] = None):
        self.config = config
        self._client = build_llm_client(config)
        self._progress = on_progress or (lambda _msg: None)

    # -- public API ---------------------------------------------------------

    def analyze(
        self,
        chapters: list[tuple[str, str]],
        title: str = "",
        author: str = "",
        id_prefix: str = "scene",
        known_characters: Optional[list[Character]] = None,
    ) -> BookAnalysis:
        """Analyze a book given a list of ``(chapter_title, chapter_text)``.

        ``id_prefix`` namespaces scene ids so chunks processed separately don't
        collide. ``known_characters`` seeds the bible from a previous chunk so
        recurring characters stay consistent (and the bible accumulates).
        """
        chunks = self._chunk(chapters)
        self._progress(f"Analyzing {len(chunks)} text chunk(s)")

        characters = self._build_character_bible(chunks, known_characters)
        self._progress(f"Identified {len(characters)} character(s)")

        scenes = self._extract_scenes(chunks, characters, id_prefix)
        self._progress(f"Extracted {len(scenes)} scene(s)")

        return BookAnalysis(
            title=title, author=author, characters=characters, scenes=scenes
        )

    def analyze_paragraphs(
        self,
        structure: AudiobookStructure,
        title: str = "",
        author: str = "",
        id_prefix: str = "scene",
        known_characters: Optional[list[Character]] = None,
    ) -> BookAnalysis:
        """One frame per paragraph (or per ``paragraphs_per_scene``), timed
        exactly to the narration via the structure's paragraph timestamps."""
        chunks = self._chunk([(ch.title, ch.text) for ch in structure.chapters])
        self._progress(f"Analyzing {len(chunks)} text chunk(s) for characters")
        characters = self._build_character_bible(chunks, known_characters)
        self._progress(f"Identified {len(characters)} character(s)")

        units = self._paragraph_units(structure, id_prefix)
        self._progress(f"Describing {len(units)} paragraph frame(s)")
        scenes = self._describe_units(units, _character_summary(characters))
        self._progress(f"Built {len(scenes)} paragraph scene(s)")

        return BookAnalysis(
            title=title, author=author, characters=characters, scenes=scenes
        )

    def _paragraph_units(self, structure: AudiobookStructure, id_prefix: str) -> list[dict]:
        """Group paragraphs into frame units with exact timestamps."""
        size = max(1, self.config.paragraphs_per_scene)
        units: list[dict] = []
        for ci, chapter in enumerate(structure.chapters, 1):
            paras = chapter.paragraphs
            for gi in range(0, len(paras), size):
                group = paras[gi : gi + size]
                text = "\n\n".join(p.text for p in group).strip()
                if not text:
                    continue
                units.append(
                    {
                        "id": f"{id_prefix}_{ci:03d}_{gi // size:04d}",
                        "chapter": chapter.title,
                        "text": text,
                        "start": group[0].start,
                        "end": group[-1].end,
                    }
                )
        return units

    def _describe_units(self, units: list[dict], char_summary: str) -> list[Scene]:
        """Batch units to the model for per-passage visual descriptions."""
        scenes: list[Scene] = []
        batch: list[dict] = []
        chars = 0
        max_chars = self.config.max_chars_per_chunk
        for unit in units:
            if batch and (chars + len(unit["text"]) > max_chars or len(batch) >= 25):
                scenes.extend(self._describe_batch(batch, char_summary))
                batch, chars = [], 0
            batch.append(unit)
            chars += len(unit["text"])
        if batch:
            scenes.extend(self._describe_batch(batch, char_summary))
        return scenes

    def _describe_batch(self, batch: list[dict], char_summary: str) -> list[Scene]:
        passages = "\n\n".join(f"[{i + 1}] {u['text']}" for i, u in enumerate(batch))
        descs: list = []
        try:
            data = self._client.complete_json(
                prompts.PARAGRAPH_SYSTEM,
                prompts.PARAGRAPH_PROMPT.format(
                    n=len(batch),
                    characters=char_summary or "(none identified)",
                    passages=passages,
                ),
                max_tokens=8192,
            )
            if isinstance(data, list):
                descs = data
        except Exception as exc:  # noqa: BLE001
            if _is_auth_error(exc):
                raise RuntimeError(
                    "Analysis authentication failed. Set a valid ANTHROPIC_API_KEY "
                    "or CLAUDE_CODE_OAUTH_TOKEN (analysis.provider: claude-code). "
                    f"Underlying error: {exc}"
                ) from exc
            self._progress(f"  (description batch failed: {exc})")

        scenes: list[Scene] = []
        for i, unit in enumerate(batch):
            item = descs[i] if i < len(descs) and isinstance(descs[i], dict) else {}
            scenes.append(
                Scene(
                    id=unit["id"],
                    chapter=unit["chapter"],
                    start_time=unit["start"],
                    end_time=unit["end"],
                    visual_description=str(item.get("visual_description", "")).strip(),
                    setting=str(item.get("setting", "")).strip(),
                    time_of_day=str(item.get("time_of_day", "")).strip(),
                    mood=str(item.get("mood", "")).strip(),
                    shot_type=str(item.get("shot_type", "")).strip(),
                    characters_present=[
                        str(c).strip()
                        for c in item.get("characters_present", [])
                        if str(c).strip()
                    ],
                    source_excerpt=unit["text"][:160],
                )
            )
        return scenes

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

    def _build_character_bible(
        self,
        chunks: list[tuple[str, str]],
        known_characters: Optional[list[Character]] = None,
    ) -> list[Character]:
        partial_lists: list[list[dict]] = []
        # Seed with the accumulated bible so recurring characters carry over and
        # are merged/updated (continuity across separately-processed chunks).
        if known_characters:
            partial_lists.append([c.model_dump() for c in known_characters])
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
                if _is_auth_error(exc):
                    raise RuntimeError(
                        "Analysis authentication failed. Check your credentials: "
                        "set a valid ANTHROPIC_API_KEY, or leave it unset and use "
                        "CLAUDE_CODE_OAUTH_TOKEN (analysis.provider: claude-code). "
                        f"Underlying error: {exc}"
                    ) from exc
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
        self,
        chunks: list[tuple[str, str]],
        characters: list[Character],
        id_prefix: str = "scene",
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
                        id=f"{id_prefix}_{i:03d}_{j:02d}",
                        chapter=chap_title or None,
                        title=str(item.get("title", "")).strip(),
                        summary=str(item.get("summary", "")).strip(),
                        setting=str(item.get("setting", "")).strip(),
                        time_of_day=str(item.get("time_of_day", "")).strip(),
                        mood=str(item.get("mood", "")).strip(),
                        shot_type=str(item.get("shot_type", "")).strip(),
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
