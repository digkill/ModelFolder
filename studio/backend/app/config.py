from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_ROOT / ".env", _ROOT.parent / ".env"),
        extra="ignore",
    )

    studio_addr: str = ":8080"
    studio_database_url: str = "postgres://studio:studio@127.0.0.1:15433/studio?sslmode=disable"
    catalog_url: str = "http://127.0.0.1:8000"
    studio_public_url: str = "http://127.0.0.1:18081/app"
    studio_orchestrator: str = "auto"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    claude_api_key: str = ""
    grok_api_key: str = ""
    xai_api_key: str = ""
    grok_base_url: str = "https://api.x.ai/v1"
    kie_api_key: str = ""
    kieai_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai/api/v1"
    meshy_api_key: str = ""
    meshy_base_url: str = "https://api.meshy.ai/openapi/v2"
    auth_username: str = ""
    auth_password: str = ""
    catalog_auth_username: str = ""
    catalog_auth_password: str = ""

    @property
    def anthropic_key(self) -> str:
        return self.anthropic_api_key or self.claude_api_key

    @property
    def grok_key(self) -> str:
        return self.grok_api_key or self.xai_api_key

    @property
    def kie_key(self) -> str:
        return self.kie_api_key or self.kieai_api_key

    @property
    def catalog(self) -> str:
        return self.catalog_url.rstrip("/")

    @property
    def kie_base(self) -> str:
        return self.kie_base_url.rstrip("/")

    @property
    def meshy_base(self) -> str:
        return self.meshy_base_url.rstrip("/")

    @property
    def openai_base(self) -> str:
        return self.openai_base_url.rstrip("/")

    @property
    def grok_base(self) -> str:
        return self.grok_base_url.rstrip("/")

    @property
    def catalog_basic(self) -> tuple[str, str] | None:
        user = self.catalog_auth_username or self.auth_username
        password = self.catalog_auth_password or self.auth_password
        if user and password:
            return (user, password)
        return None

    @property
    def dsn(self) -> str:
        url = self.studio_database_url
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://") :]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
