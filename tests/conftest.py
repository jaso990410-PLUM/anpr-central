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
os.environ.setdefault("ANPR_MQTT_ENABLED", "false")
os.environ.setdefault("ANPR_MQTT_USERNAME", "")
os.environ.setdefault("ANPR_MQTT_PASSWORD", "")

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402, F401
from app.services.mqtt_config_service import environment_mqtt_config  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_mqtt_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module,
        "_startup_mqtt_configuration",
        environment_mqtt_config,
    )


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
