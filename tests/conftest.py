import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


os.environ.setdefault("ANPR_DB_PASSWORD", "test-database-password")
os.environ.setdefault(
    "ANPR_SECRET_KEY",
    "test-only-secret-key-with-at-least-32-characters",
)

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402, F401


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        yield session

    Base.metadata.drop_all(engine)
    engine.dispose()
