#!/usr/bin/env python3
"""Create the database and seed the 1000-problem LeetCode catalog.

Usage (from KT_python/):
    PYTHONPATH=pylib:. python3 scripts/seed_db.py [database_url]

Defaults to sqlite:///./kt.db and the catalog at ../data/seed/catalog_1000.json
(override with KT_DATABASE_URL / KT_CATALOG_PATH).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running from anywhere

from knowledge_tracer.config import Settings
from knowledge_tracer.db import create_schema, make_engine, make_session_factory
from knowledge_tracer.seed import load_catalog


def main() -> None:
    settings = Settings.from_env()
    if len(sys.argv) > 1:
        settings.database_url = sys.argv[1]
    engine = make_engine(settings.database_url)
    create_schema(engine)
    session = make_session_factory(engine)()
    try:
        report = load_catalog(session, settings)
        session.commit()
    finally:
        session.close()
    if report.skipped:
        print("Catalog already loaded; nothing to do.")
    else:
        print(f"Seeded {report.concepts} concepts, {report.prerequisites} prerequisite edges, "
              f"{report.problems} problems, {report.problem_concepts} concept tags "
              f"into {settings.database_url}")


if __name__ == "__main__":
    main()
