"""Web UI (Django + Django Ninja) for browsing, editing and re-rendering shots."""

from .app import create_app

__all__ = ["create_app"]
