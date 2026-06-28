"""Shared utilities."""

import re


def slugify(name: str) -> str:
    """Filesystem-safe slug for a book/input name (used for output subfolders)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "book"


__all__ = ["slugify"]
