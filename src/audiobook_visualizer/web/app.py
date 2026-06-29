"""Build the Django ASGI application that serves the API + the built SPA.

The urlconf is assembled in-process (no settings module / urls.py file) so the
package stays embeddable: one app per process, pointed at one output dir.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Optional

from ..config import Config
from .django_conf import URLCONF_MODULE, django_setup

_PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8">
<title>Audiobook Visualizer</title></head>
<body style="font-family:system-ui;background:#111;color:#eee;padding:2rem">
<h1>Audiobook Visualizer API is running</h1>
<p>The web UI bundle was not found. Build the frontend:</p>
<pre style="background:#000;padding:1rem;border-radius:8px">cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>The JSON API is available under <a style="color:#7ad" href="/api/state">/api/…</a>.</p>
</body></html>"""


def create_app(out_dir: str | Path, config: Optional[Config] = None, dry_run: bool = False):
    """Return the Django ASGI application for one output dir."""
    django_setup()
    from django.core.asgi import get_asgi_application

    from .studio import Studio

    studio = Studio(out_dir, config or Config.load(), dry_run=dry_run)
    _install_urlconf(studio)
    return get_asgi_application()


def _install_urlconf(studio) -> None:
    from django.http import FileResponse, HttpResponse
    from django.urls import path, re_path
    from django.views.static import serve as static_serve

    from .api import build_api

    api = build_api(studio)
    dist = studio.dist_dir

    def index(_request):
        if dist and (dist / "index.html").exists():
            return FileResponse(open(dist / "index.html", "rb"))
        return HttpResponse(_PLACEHOLDER)

    patterns = [
        path("api/", api.urls),
        re_path(r"^frames/(?P<path>.*)$", static_serve, {"document_root": str(studio.frames_dir)}),
        re_path(r"^portraits/(?P<path>.*)$", static_serve,
                {"document_root": str(studio.portraits_dir)}),
    ]
    if dist and (dist / "assets").exists():
        patterns.append(re_path(r"^assets/(?P<path>.*)$", static_serve,
                                {"document_root": str(dist / "assets")}))
    patterns.append(re_path(r"^.*$", index))  # SPA catch-all

    module = types.ModuleType(URLCONF_MODULE)
    module.urlpatterns = patterns
    sys.modules[URLCONF_MODULE] = module
