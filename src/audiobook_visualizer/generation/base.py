"""Image provider interface.

Adding a new image backend (Replicate, local Stable Diffusion, Gemini, ...) is
just implementing this one method and registering it in ``factory.py``. The rest
of the pipeline is provider-agnostic.
"""

from __future__ import annotations

import abc
from pathlib import Path


class ImageProvider(abc.ABC):
    """Renders a text prompt to an image file on disk."""

    name: str = "base"
    model: str = ""

    @abc.abstractmethod
    def generate(self, prompt: str, out_path: Path) -> Path:
        """Render ``prompt`` to ``out_path`` and return the written path.

        Implementations should raise on failure; the caller records the error
        against the frame and continues with the next scene.
        """
        raise NotImplementedError
