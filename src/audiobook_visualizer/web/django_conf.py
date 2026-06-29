"""Minimal embedded Django configuration.

Django is used purely as the web/API layer (Django Ninja for the JSON API, the
static views to serve the built SPA + frame images). There are no Django ORM
models or migrations — all domain data lives in the per-book ``production.db``
via :class:`ProductionStore`. ``django-ninja`` reads settings at import time, so
:func:`django_setup` must run before any ``ninja`` import.
"""

from __future__ import annotations

URLCONF_MODULE = "audiobook_visualizer.web._urlconf"


def django_setup() -> None:
    from django.conf import settings

    if settings.configured:
        return
    settings.configure(
        DEBUG=False,
        SECRET_KEY="audiobook-visualizer-local",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=URLCONF_MODULE,
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
        DATABASES={},  # no Django ORM — domain data is in production.db
        USE_TZ=True,
        LOGGING_CONFIG=None,
    )
    import django

    django.setup()
