from pathlib import Path

from pydantic import Field, SecretStr
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
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_client_id: str = "anpr-central"
    mqtt_enabled: bool = True
    mqtt_heartbeat_timeout_seconds: int = Field(default=45, gt=0)
    visionzenith_bv: int = Field(default=17932, ge=0)
    visionzenith_serial_channel: int = Field(default=0, ge=0, le=1)
    visionzenith_command_timeout_seconds: int = Field(default=10, gt=0)
    visionzenith_auto_led_enabled: bool = True
    control_board_runtime_interval_seconds: int = Field(default=30, ge=5, le=300)
    recognition_media_dir: Path = Path("var/recognition-media")
    recognition_correlation_seconds: int = Field(default=10, ge=1, le=120)
    recognition_page_size: int = Field(default=50, ge=10, le=100)
    recognition_max_image_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024
    )
    offline_recovery_batch_size: int = Field(default=100, ge=1, le=1000)
    offline_recovery_timeout_seconds: int = Field(default=60, ge=10, le=900)
    live_poll_seconds: int = Field(default=5, ge=3, le=30)

    default_site_code: str = "MAIN"

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
