#!/usr/bin/env python3
"""Run the Knowledge Tracer Python API (FastAPI + uvicorn).

Usage (from anywhere):
    python3 scripts/run_server.py

Then open http://127.0.0.1:8081/dashboard.html for the learner dashboard.

Environment variables:
    KT_HOST          bind host (default "127.0.0.1")
    KT_PORT          bind port (default 8081 — the Java backend uses 8080, so the
                     two can run side by side)
    KT_DATABASE_URL  SQLAlchemy database URL (default "sqlite:///./kt_python.db",
                     set here before the app factory runs; the library default in
                     knowledge_tracer/config.py is "sqlite:///./kt.db")
    KT_API_KEY       shared API key required on every /api/** request via the
                     X-API-Key header; blank (the default) disables the check
                     for local development

Further feature flags (KT_IRT_ENABLED, KT_DKT_ENABLED, KT_JEV_PROVIDER,
KT_RESEARCH_EXPORT, KT_PSEUDONYM_SALT, KT_CATALOG_PATH, ...) are read from the
environment by knowledge_tracer.config.Settings.from_env().
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # KT_python/

# Make the project and its pip dependencies importable no matter where
# the interpreter was launched from.
sys.path.insert(0, str(ROOT))
venv_site = ROOT / ".venv" / "Lib" / "site-packages"
if venv_site.is_dir():
    sys.path.insert(0, str(venv_site))
elif sys.platform != "win32":
    sys.path.insert(0, str(ROOT / "pylib"))

# Default database for the launcher: a project-named SQLite file, distinct from the
# library default (kt.db) so the server's store is unambiguous.
# An explicit KT_DATABASE_URL always wins.
os.environ.setdefault("KT_DATABASE_URL", "sqlite:///./kt_python.db")


def main() -> None:
    import uvicorn

    host = os.environ.get("KT_HOST", "127.0.0.1")
    port = int(os.environ.get("KT_PORT", "8081"))
    print(f"Knowledge Tracer Python API on http://{host}:{port} "
          f"(dashboard: http://{host}:{port}/dashboard.html)", flush=True)
    uvicorn.run("knowledge_tracer.api.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
