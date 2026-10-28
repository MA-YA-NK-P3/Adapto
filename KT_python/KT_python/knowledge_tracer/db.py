"""Database plumbing: engine, session factory, declarative base.

The Java side gets this from Spring Data JPA + Flyway. Here SQLAlchemy creates the
schema from the entities in domain.py (there are no migrations in the replica; the
entities ARE the schema, mirroring what the V1-V6 Flyway migrations produce).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all entities in domain.py."""


def make_engine(url: str) -> Engine:
    # check_same_thread=False lets the FastAPI app share a SQLite connection across
    # worker threads; it is a no-op for other databases.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    # expire_on_commit=False: services return entities after commit (Spring's
    # open-in-view=false equivalent — everything needed is loaded in the transaction).
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    from . import domain  # noqa: F401  (register entities before create_all)

    Base.metadata.create_all(engine)


@contextmanager
def transaction(session: Session) -> Iterator[Session]:
    """One atomic unit of work, the @Transactional equivalent.

    Event ingestion, the state transition it causes, the recommendation reward and
    the bandit update commit or roll back together (master plan §54).
    """
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
