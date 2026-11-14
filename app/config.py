"""Settings, read from environment / .env. See .env.example."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

MARKET_TZ = ZoneInfo("Australia/Sydney")
VALID_UNIVERSES = ("asx50", "asx100", "asx200", "asx300")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/app/data")
    app_port: int = 8000
    app_password: str = ""
    secret_key: str = ""

    index_universe: str = "asx200"

    brokerage_per_trade: float = 5.0
    slippage_bps: float = 10.0
    paper_capital: float = 50_000.0
    backtest_top_n: int = 10
    backtest_start: str = "2020-01-01"

    announcement_backfill_years: int = 10
    asic_backfill_years: int = 10
    director_trades_backfill_days: int = 365

    rate_limit_seconds: float = 1.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130 Safari/537.36 ASXAnalyst/0.1 (personal, non-commercial)"
    )

    bing_news_enabled: bool = False
    llm_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"
    llm_scope: str = "followed"       # followed (watchlist + holdings) | universe (every index member)
    llm_daily_limit: int = 40         # max AI summaries per 24 hours

    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_token: str = ""
    webhook_url: str = ""
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    alert_signals: bool = True        # signal change on a watchlist/held stock
    alert_announcements: bool = True  # price-sensitive announcement for a watchlist/held stock
    alert_jobs: bool = True           # a scheduled job failed
    app_url: str = ""                 # e.g. http://<server-ip>:8000, for links in alerts

    backup_keep: int = 4             # weekly automatic DB backups kept in data/backups (0 = off)

    log_level: str = "INFO"
    scheduler_enabled: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "asx.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def universe(self) -> str:
        u = self.index_universe.strip().lower().replace("/", "").replace(" ", "")
        return u if u in VALID_UNIVERSES else "asx200"

    def get_secret_key(self) -> str:
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        path.write_text(key)
        path.chmod(0o600)
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
