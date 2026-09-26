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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ANPR_",
        extra="ignore",
    )


settings = Settings()