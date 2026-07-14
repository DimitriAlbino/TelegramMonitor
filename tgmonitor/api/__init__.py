"""FastAPI application — web API + health.

T1 scope: a liveness endpoint (``GET /healthz`` — the off-box-probe surface
from the reference pattern) and one unauthenticated read endpoint returning
recent Results for a Monitor. Auth, CRUD, and the Telegram webhook arrive in
later tickets and slot in here.
"""

from __future__ import annotations

from .main import app, create_app

__all__ = ["app", "create_app"]
