"""Web UI — server-rendered HTML pages (Jinja2) served by FastAPI.

The web UI (ADR-0010) lives on the same FastAPI process as the JSON API and the
Telegram webhook. Templates are under ``templates/``; static assets under
``static/``. Pages authenticate via a cookie session (not the Bearer header).
"""
