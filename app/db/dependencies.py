from collections.abc import Generator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


def get_db() -> Generator[Session, None, None]:
    """Provide one SQLAlchemy session for the lifetime of a request."""
    with SessionLocal() as session:
        yield session
