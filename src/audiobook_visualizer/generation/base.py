"""Image provider interface.

Adding a new image backend (Replicate, local Stable Diffusion, ...) is just
implementing this one method and registering it in ``factory.py``. The rest of
the pipeline is provider-agnostic.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Optional, Sequence


class ImageProvider(abc.ABC):
    """Renders a text prompt to an image file on disk."""

    name: str = "base"
    model: str = ""
    # Whether the backend can condition on reference images (e.g. character
    # portraits) for cross-frame consistency. Providers that can't simply
    # ignore the ``references`` argument.
    supports_references: bool = False

    @abc.abstractmethod
    def generate(
        self,
        prompt: str,
        out_path: Path,
        references: Optional[Sequence[Path]] = None,
    ) -> Path:
        """Render ``prompt`` to ``out_path`` and return the written path.

        ``references`` are optional image paths to condition on (used by
        providers where ``supports_references`` is True). Implementations should
        raise on failure; the caller records the error against the frame and
        continues with the next scene.
        """
        raise NotImplementedError
