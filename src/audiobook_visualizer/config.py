"""Configuration loading.

Config is layered: built-in defaults <- optional YAML file <- nothing else.
Secrets (API keys) come only from the environment, never the YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    style: str = (
        "cinematic concept art, painterly, dramatic lighting, muted color "
        "palette, detailed, atmospheric, film still, 16:9"
    )


class AnalysisConfig(BaseModel):
    # auto: prefer ANTHROPIC_API_KEY, else the Claude Code CLI
    # (CLAUDE_CODE_OAUTH_TOKEN). Force with "anthropic" or "claude-code".
    provider: str = "auto"
    model: str = "claude-sonnet-4-6"
    # "scenes": Claude picks key visual moments per chunk. "paragraphs": one
    # frame per paragraph (or per `paragraphs_per_scene` paragraphs), timed
    # exactly to the narration — needs an audiobook structure. Most narration-
    # synced, but more frames/cost.
    mode: str = "scenes"
    paragraphs_per_scene: int = 1
    # Smaller chunks + more scenes => denser frames that track the narration more
    # closely (a frame every ~30s rather than every minute-plus). Raise/lower to
    # trade pacing against cost.
    max_chars_per_chunk: int = 8000
    scenes_per_chunk: int = 5
    temperature: float = 0.4


class AudioConfig(BaseModel):
    enabled: bool = True
    backend: str = "faster-whisper"  # or "openai"
    model: str = "base"
    align_to_ebook: bool = True
    # Stream long audio through ffmpeg into on-disk chunks of this many seconds
    # and transcribe them one at a time, so memory stays bounded regardless of
    # book length. 0 disables chunking (loads the whole file — only for tiny
    # inputs). Files shorter than this are transcribed in one pass.
    chunk_seconds: int = 600
    # Audiobook-only segmentation (used when no ebook is supplied).
    segment: bool = True
    chapter_mode: str = "auto"  # auto | files | markers | headings | time | single
    chapter_seconds: int = 900  # window length for the "time" fallback
    paragraph_gap: float = 0.65  # pause (s) that can end a paragraph
    paragraph_max_chars: int = 1500  # hard cap so paragraphs stay bounded


class GenerationConfig(BaseModel):
    # auto: pick from available keys (OPENAI_API_KEY -> openai, else
    # GEMINI_API_KEY -> gemini). Force with "openai" | "gemini" | "stub".
    provider: str = "auto"
    model: str = "dall-e-3"
    size: str = "1792x1024"
    quality: str = "standard"
    max_frames: int = 0
    concurrency: int = 3
    # Reuse unchanged frames across runs (skip the API call when inputs match).
    cache: bool = True
    # Generate a reference portrait per character and feed it to the image model
    # for cross-frame consistency. Only effective for providers that support
    # reference images (e.g. gemini); ignored otherwise.
    character_portraits: bool = True
    # Portraits are head-and-shoulders references — square avoids the widescreen
    # crop that would cut off the face.
    portrait_size: str = "1024x1024"
    # Include the analyzer's per-scene shot type (wide/medium/close-up) in prompts.
    shot_variety: bool = True


class OutputConfig(BaseModel):
    dir: str = "output"
    gallery: bool = True


class Config(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        """Load config from a YAML file, falling back to defaults."""
        if path is None:
            # Auto-discover a config.yaml in the working directory.
            candidate = Path("config.yaml")
            path = candidate if candidate.exists() else None
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)


def load_env(env_path: Optional[str | Path] = None) -> None:
    """Minimal .env loader (avoids a python-dotenv dependency).

    Only sets variables that are not already present in the environment, so
    real environment variables always win.
    """
    candidate = Path(env_path) if env_path else Path(".env")
    if not candidate.exists():
        return
    for raw in candidate.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Copy .env.example to .env and fill it in."
        )
    return value
