"""Provider factory: map config to a concrete :class:`ImageProvider`."""

from __future__ import annotations

import os

from ..config import GenerationConfig
from .base import ImageProvider
from .stub_provider import StubImageProvider


def _has_key(name: str) -> bool:
    """True for a real credential (ignores empty/placeholder '...' values)."""
    value = os.environ.get(name, "")
    return bool(value.strip()) and "..." not in value


def _resolve_provider(provider: str, model: str) -> str:
    """Resolve ``auto`` to a concrete provider based on available credentials."""
    provider = (provider or "auto").lower()
    if provider != "auto":
        return provider
    # If the model is explicitly a Gemini model, prefer Gemini.
    if "gemini" in (model or "").lower() and _has_key("GEMINI_API_KEY"):
        return "gemini"
    if _has_key("OPENAI_API_KEY"):
        return "openai"
    if _has_key("GEMINI_API_KEY"):
        return "gemini"
    raise RuntimeError(
        "No image-generation credentials found. Set OPENAI_API_KEY (DALL-E) or "
        "GEMINI_API_KEY (Gemini), or set generation.provider explicitly."
    )


def get_provider(config: GenerationConfig, dry_run: bool = False) -> ImageProvider:
    """Return the image provider selected by config.

    ``dry_run`` forces the offline stub provider regardless of config, so the
    pipeline can run end-to-end without API access.
    """
    if dry_run:
        return StubImageProvider(size=config.size)

    provider = _resolve_provider(config.provider, config.model)
    if provider == "openai":
        from .openai_provider import OpenAIImageProvider

        return OpenAIImageProvider(
            model=config.model, size=config.size, quality=config.quality
        )
    if provider in ("gemini", "nano-banana", "nanobanana"):
        from .gemini_provider import GeminiImageProvider

        # The shared `model` default is a DALL-E id; pick a Gemini model unless
        # the user explicitly set a gemini-* model.
        model = config.model if "gemini" in config.model.lower() else ""
        return GeminiImageProvider(model=model, size=config.size)
    if provider == "stub":
        return StubImageProvider(size=config.size)
    raise ValueError(
        f"Unknown image provider {config.provider!r}. "
        "Supported: 'auto', 'openai', 'gemini', 'stub' (or use --dry-run)."
    )
