from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=".env", extra="ignore")

    database_url: SecretStr
    secret: SecretStr
    public_url: str = "http://127.0.0.1:8765"
    environment: str = "development"
    mail_mode: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    bootstrap_token: SecretStr = SecretStr("")
    access_seconds: int = 300
    refresh_seconds: int = 604800
    enrollment_seconds: int = 1800
    otp_seconds: int = 600
    proof_seconds: int = 60
    artifact_directory: str = ""
    artifact_project_bytes: int = 1073741824
    artifact_total_bytes: int = 17179869184

    @model_validator(mode="after")
    def validate_security(self):
        if len(self.secret.get_secret_value()) < 32:
            raise ValueError("HUB_SECRET must contain at least 32 characters")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("Unknown environment")
        url = urlsplit(self.public_url)
        if not url.hostname or url.query or url.fragment or url.username or url.path.rstrip("/"):
            raise ValueError("HUB_PUBLIC_URL must be an origin without a path or credentials")
        if url.scheme != "https" and not (
            self.environment != "production" and url.hostname in {"127.0.0.1", "localhost", "testserver"}
        ):
            raise ValueError("HTTPS is required except for local development")
        if self.mail_mode not in {"smtp", "memory"}:
            raise ValueError("Unknown mail mode")
        if self.mail_mode == "memory" and self.environment != "test":
            raise ValueError("Memory mail is only available in tests")
        if self.bootstrap_token.get_secret_value() and len(self.bootstrap_token.get_secret_value()) < 32:
            raise ValueError("HUB_BOOTSTRAP_TOKEN must contain at least 32 characters")
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("Choose SMTP TLS or STARTTLS, not both")
        if self.environment == "production" and self.smtp_host and not (self.smtp_starttls or self.smtp_ssl):
            raise ValueError("Production SMTP requires STARTTLS")
        for field in (
            "access_seconds",
            "refresh_seconds",
            "enrollment_seconds",
            "otp_seconds",
            "proof_seconds",
            "artifact_project_bytes",
            "artifact_total_bytes",
        ):
            if getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
