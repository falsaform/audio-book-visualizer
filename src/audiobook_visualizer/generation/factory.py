"""Provider factory: map config to a concrete :class:`ImageProvider`."""

from __future__ import annotations

from ..config import GenerationConfig
from .base import ImageProvider
from .stub_provider import StubImageProvider


def get_provider(config: GenerationConfig, dry_run: bool = False) -> ImageProvider:
    """Return the image provider selected by config.

    ``dry_run`` forces the offline stub provider regardless of config, so the
    pipeline can run end-to-end without API access.
    """
    if dry_run:
        return StubImageProvider(size=config.size)

    provider = config.provider.lower()
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
        return GeminiImageProvider(model=model)
    if provider == "stub":
        return StubImageProvider(size=config.size)
    raise ValueError(
        f"Unknown image provider {config.provider!r}. "
        "Supported: 'openai', 'gemini', 'stub' (or use --dry-run)."
    )
