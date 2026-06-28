"""Generation: turn scenes into image prompts and rendered still frames."""

from .base import ImageProvider
from .factory import get_provider
from .prompt_builder import build_prompt

__all__ = ["ImageProvider", "get_provider", "build_prompt"]
