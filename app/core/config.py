from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ANPR Central"
    app_version: str = "0.1.0"

    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "anpr_db"
    db_user: str = "anpr_app"
    db_password: str

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883

    secret_key: str = Field(min_length=32)
    session_cookie_name: str = "anpr_session"
    csrf_cookie_name: str = "anpr_login_csrf"
    csrf_lifetime_minutes: int = Field(default=30, gt=0)
    session_lifetime_minutes: int = Field(default=480, gt=0)
    session_idle_minutes: int = Field(default=30, gt=0)
    cookie_secure: bool = False
    login_max_failures: int = Field(default=5, gt=0)
    login_lock_minutes: int = Field(default=15, gt=0)
    enable_docs: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ANPR_",
        extra="ignore",
    )


settings = Settings()
